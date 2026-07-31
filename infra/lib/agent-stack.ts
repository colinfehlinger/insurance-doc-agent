import * as path from 'path';
import * as fs from 'fs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface AgentStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
  readonly matterTable: dynamodb.ITable;
}

/**
 * The brain -- the only part of the system allowed to exercise judgment.
 *
 * The agent does not classify or extract. It reads a matter's state and decides
 * what happens next, and everything it can actually do is a tool call, so the
 * blast radius is the tool list, not the model (agent/tools/README.md).
 *
 * Step 6 thin slice: ONE tool (escalate_to_human) and ONE matter. Built natively
 * on the stable AgentCore L1s in aws-cdk-lib 2.261.0 -- no alpha dependency, no
 * separate sub-project (ADR-006). The managed Harness is declared as config;
 * tools attach through a Gateway; every tool call is logged by the Gateway and
 * traced by Observability. Cedar Policy and the other four tools are reserved
 * for later passes (docs/step-6-agent-design.md).
 */
export class AgentStack extends cdk.Stack {
  public readonly harness: agentcore.CfnHarness;
  public readonly escalationTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);

    const { config, dataKey, matterTable } = props;
    const metricNamespace = `Ida/${config.stage}/Agent`;

    // --- Escalation destination ---------------------------------------------
    // SNS, deliberately: it needs no verified SES sender (SNS owns delivery), so
    // escalate_to_human sidesteps the SES sender-identity gap that gates
    // send_reminder (ADR-005). Email subscription to the internal owner.
    this.escalationTopic = new sns.Topic(this, 'EscalationTopic', {
      topicName: `${config.resourcePrefix}-escalations`,
      masterKey: dataKey, // encrypt notifications at rest with the shared CMK
    });
    this.escalationTopic.addSubscription(
      new subscriptions.EmailSubscription(config.messaging.testRecipient),
    );

    // --- The one tool: escalate_to_human Lambda -----------------------------
    // Own the log group explicitly. With `useCdkManagedLogGroup` on, a Function
    // otherwise gets a CDK-managed log group that defaults to RETAIN -- so on a
    // stack destroy/rollback the group `/aws/lambda/<fn>` is orphaned, and the
    // next deploy's create collides with the survivor ("already exists"). Making
    // CDK the single, DESTROY-on-delete owner (named to the conventional path so
    // the Lambda uses it rather than auto-creating a duplicate) ends that cycle.
    // This is the deferred Step-5 log-group bug.
    const escalateLogGroup = new logs.LogGroup(this, 'EscalateFnLogGroup', {
      logGroupName: `/aws/lambda/${config.resourcePrefix}-tool-escalate`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const escalateFn = new lambda.Function(this, 'EscalateFn', {
      functionName: `${config.resourcePrefix}-tool-escalate`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'tools', 'escalate')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logGroup: escalateLogGroup,
      environment: {
        MATTER_TABLE: matterTable.tableName,
        ESCALATION_TOPIC_ARN: this.escalationTopic.topicArn,
        METRIC_NAMESPACE: metricNamespace,
      },
    });
    // Least privilege: append matter actions, publish escalations, decrypt with
    // the CMK, emit metrics. Note grantReadWriteData is broader than the
    // append-only contract; the append-only guarantee is enforced by the tool's
    // code (conditional put, no field overwrite), and will be backstopped by
    // Cedar when send_reminder lands (ADR-006 guardrail split).
    matterTable.grantReadWriteData(escalateFn);
    this.escalationTopic.grantPublish(escalateFn);
    dataKey.grantEncryptDecrypt(escalateFn);
    escalateFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['cloudwatch:PutMetricData'], resources: ['*'] }),
    );

    // --- Gateway: the only way the agent can act ----------------------------
    // The gateway holds the credentials and invokes tool Lambdas; the agent
    // never does. AWS_IAM inbound auth means the Harness's execution role is what
    // authorizes calls to the gateway.
    const gatewayRole = new iam.Role(this, 'GatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'AgentCore Gateway role -- invokes the tool Lambdas on the agent behalf.',
    });
    escalateFn.grantInvoke(gatewayRole);

    const gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: `${config.resourcePrefix}-gateway`,
      roleArn: gatewayRole.roleArn,
      protocolType: 'MCP',
      authorizerType: 'AWS_IAM',
      description: 'Document-Chase Agent tool gateway (Step 6 slice: escalate_to_human only).',
    });

    // --- Gateway target: escalate_to_human, with its typed tool schema -------
    // NOTE the naming split, confirmed against the bedrock-agentcore-control
    // service model: the TARGET resource `name` must match TargetName
    // (([0-9a-zA-Z][-]?){1,100} -- hyphens allowed, NO underscores), so it is
    // hyphenated; the TOOL the agent actually invokes is the ToolDefinition
    // `name` below (unconstrained String), which stays escalate_to_human and is
    // what `allowedTools` on the Harness references. Description max is 200.
    new agentcore.CfnGatewayTarget(this, 'EscalateTarget', {
      gatewayIdentifier: gateway.attrGatewayIdentifier,
      name: 'escalate-to-human',
      description:
        'Hand a matter to a human for review, with the reason. Use when the correct next action is not automatable: a required document is overdue, the matter is inconsistent, or the right action is unclear.',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: escalateFn.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'escalate_to_human',
                  description:
                    'Escalate a matter to a human owner with a reason. Records the escalation on the matter (audit trail) and notifies the owner.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      matterId: { type: 'string', description: 'The matter to escalate, e.g. MTR-2026-0142.' },
                      docType: {
                        type: 'string',
                        description: 'The document the escalation concerns, e.g. census. Optional; defaults to the matter as a whole.',
                      },
                      reason: {
                        type: 'string',
                        description:
                          'Why this needs a human, in terms of the matter state relied on: which document, which dates, which prior actions.',
                      },
                    },
                    required: ['matterId', 'reason'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Allow the gateway service to invoke the tool Lambda on the gateway's behalf.
    escalateFn.addPermission('AllowAgentCoreGateway', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
    });

    // --- Harness: the agent, as configuration -------------------------------
    // System prompt is loaded from agent/system-prompt.md so the prompt lives in
    // one reviewable place (a change to it is a change to the control environment).
    const systemPrompt = fs.readFileSync(
      path.join(__dirname, '..', '..', 'agent', 'system-prompt.md'),
      'utf-8',
    );

    // Harness name is reused below for the workload-identity resource ARN, so
    // it is a const rather than inline. Harness naming forbids hyphens (<=40
    // chars), hence the underscore transform.
    const harnessName = `${config.resourcePrefix.replace(/-/g, '_')}_doc_chase_agent`;

    const harnessRole = new iam.Role(this, 'HarnessRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'AgentCore Harness execution role -- invokes the model, the gateway, and the base AgentCore primitives.',
    });
    // Model access: build on Claude Sonnet 4.6 (ADR-001 -- build strong, eval
    // down). Sonnet 4.6 is INFERENCE_PROFILE-only (confirmed against Bedrock:
    // the bare `anthropic.claude-sonnet-4-6` foundation model has
    // inferenceTypesSupported=[INFERENCE_PROFILE], and there is NO date suffix
    // -- unlike 4.5's ...-20250929-v1:0). So the harness model id is the US
    // cross-region inference profile `us.anthropic.claude-sonnet-4-6`, and IAM
    // must allow InvokeModel on BOTH the profile ARN and the foundation-model
    // ARNs it fans out to across US regions.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-6`,
        ],
      }),
    );
    // Call the gateway at runtime. bedrock-agentcore:InvokeGateway is the runtime
    // gateway-invocation action, authorized on the Gateway ARN. Confirmed the
    // managed Harness DOES use this execution role (not a service-side broker):
    // without the base permissions below the harness could not obtain a workload
    // token to reach the gateway, and tool discovery failed silently. Cedar Policy
    // actions belong on the GATEWAY role only when a Policy Engine exists, which
    // the slice defers (ADR-006 guardrail split).
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [gateway.attrGatewayArn, `${gateway.attrGatewayArn}/*`],
      }),
    );

    // --- BASE AgentCore Harness execution-role contract ---------------------
    // The managed Harness has an IMPLICIT base-permission contract the AgentCore
    // CLI auto-provisions but CDK does NOT. A self-built role must carry it, or
    // the harness fails SILENTLY: without the workload-identity token it cannot
    // authenticate to the Gateway to discover tools, so it builds the model
    // request with an EMPTY toolConfig -- the model reasons correctly but has no
    // tool to call (stopReason=end_turn, no tool, no side effect). This was the
    // Step-6 "silent empty-toolConfig" failure. Source:
    // docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html
    // (sample execution-role policy). Least-privilege scoped below.

    // Workload Identity -- the token the harness uses to reach the gateway (and
    // any AgentCore primitive). THE fix for tool discovery. Resources confirmed
    // to exist (auto-created by the harness deploy): workload-identity-directory
    // /default and .../workload-identity/harness_<name>-<hash>.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:GetWorkloadAccessToken',
          'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/harness_${harnessName}-*`,
        ],
      }),
    );

    // X-Ray -- without this the harness emits no traces (this is why the Step-6
    // Observability audit-trail leg was empty). Sampling reads are account-wide.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'xray:PutTraceSegments',
          'xray:PutTelemetryRecords',
          'xray:GetSamplingRules',
          'xray:GetSamplingTargets',
        ],
        resources: ['*'],
      }),
    );

    // CloudWatch Logs -- the harness runtime writes under /aws/bedrock-agentcore/runtimes/*.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
      }),
    );
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:DescribeLogGroups'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:*`],
      }),
    );

    // CloudWatch metrics -- scoped to the bedrock-agentcore namespace.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
      }),
    );

    // ECR Public -- the harness pulls its application container from ECR Public
    // at the start of each session; both actions require Resource "*".
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['ecr-public:GetAuthorizationToken', 'sts:GetServiceBearerToken'],
        resources: ['*'],
      }),
    );

    this.harness = new agentcore.CfnHarness(this, 'Harness', {
      // HarnessName pattern: [a-zA-Z][a-zA-Z0-9_]{0,39} -- NO hyphens, <=40 chars
      // (the opposite of the gateway/target rule). Underscores; 23 chars.
      harnessName,
      executionRoleArn: harnessRole.roleArn,
      model: {
        bedrockModelConfig: {
          // US cross-region inference profile for Claude Sonnet 4.6 -- CONFIRMED
          // ACTIVE via list-inference-profiles. Sonnet 4.6 has no date-suffixed
          // foundation-model id and is invocable only through a profile. (`us.`
          // keeps inference within US regions, consistent with the project's
          // data posture; `global.anthropic.claude-sonnet-4-6` is the wider
          // alternative.)
          modelId: 'us.anthropic.claude-sonnet-4-6',
          maxTokens: 2048,
          temperature: 0,
        },
      },
      systemPrompt: [{ text: systemPrompt }],
      tools: [
        {
          // `type` enum CONFIRMED against the CloudFormation resource reference
          // for AWS::BedrockAgentCore::Harness HarnessTool -- allowed values:
          // remote_mcp | agentcore_browser | agentcore_gateway |
          // inline_function | agentcore_code_interpreter. (The L1 type def only
          // says `string`; the docs pin it. Lowercase snake_case.)
          type: 'agentcore_gateway',
          // Harness-side name -- harness naming forbids hyphens (opposite of the
          // gateway target), so underscores. Not the agent-visible tool name.
          name: 'gateway_tools',
          config: {
            agentCoreGateway: {
              gatewayArn: gateway.attrGatewayArn,
              outboundAuth: { awsIam: {} },
            },
          },
        },
      ],
      // The Gateway exposes a tool as `{targetName}___{toolName}` -- confirmed
      // empirically via an MCP tools/list against the live gateway (2026-07-27):
      // the target `escalate-to-human` + tool `escalate_to_human` surface as
      // `escalate-to-human___escalate_to_human`. allowedTools filters on that
      // exposed name, so the bare tool name would match nothing and leave the
      // agent with no tools. (When the tool set grows, this allowlist becomes a
      // real guardrail alongside Cedar; for one tool it also documents the
      // composition rule in code.)
      allowedTools: ['escalate-to-human___escalate_to_human'],
      maxIterations: 8,
      // Memory DISABLED, on purpose and for two reasons at once:
      //  1. The design defers AgentCore Memory to the multi-touch cadence pass --
      //     durable per-matter state already lives in the matter table, which is
      //     what the agent reasons over. A single decision needs no session memory.
      //  2. Leaving `memory` unset makes the managed Harness auto-provision a
      //     memory resource, and the execution role then needs bedrock-agentcore
      //     event/memory permissions on it. Without them the agentic loop breaks
      //     at start-up with AccessDenied on ListEvents -- the model still emits
      //     the correct tool-use, but the loop never executes the tool (found the
      //     hard way: the escalate Lambda showed zero invocations). Disabling
      //     memory removes both the resource and the permission surface.
      // When Memory is (re)introduced later, it comes back with its own scoped
      // event/memory grants on the memory ARN.
      memory: { disabled: {} },
    });

    // --- SSM contract + messaging config ------------------------------------
    // Replaces the probe's runtime-arn placeholder with the real harness ARN.
    new ssm.StringParameter(this, 'AgentHarnessArnParam', {
      parameterName: `${config.ssmPrefix}/agent/harness-arn`,
      stringValue: this.harness.attrArn,
      description: 'ARN of the AgentCore Harness that decides the next action per matter.',
    });

    // Messaging config for send_reminder (arrives in a later pass). Both point at
    // the only verified SES identity for now (config.ts); they move to the real
    // domain once it is SES-verified (ADR-005 domain track).
    new ssm.StringParameter(this, 'ReminderRecipientParam', {
      parameterName: `${config.ssmPrefix}/messaging/test-recipient`,
      stringValue: config.messaging.testRecipient,
      description: 'Where send_reminder delivers in this stage (dev: single verified test recipient).',
    });
    new ssm.StringParameter(this, 'ReminderSenderParam', {
      parameterName: `${config.ssmPrefix}/messaging/sender-address`,
      stringValue: config.messaging.senderAddress,
      description: 'From address for send_reminder. Must be a verified SES identity before it can send.',
    });

    new cdk.CfnOutput(this, 'HarnessArn', { value: this.harness.attrArn });
    new cdk.CfnOutput(this, 'GatewayArn', { value: gateway.attrGatewayArn });
    new cdk.CfnOutput(this, 'EscalationTopicArn', { value: this.escalationTopic.topicArn });
    new cdk.CfnOutput(this, 'EscalateFnName', { value: escalateFn.functionName });
  }
}
