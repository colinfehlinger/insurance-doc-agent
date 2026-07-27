import * as path from 'path';
import * as fs from 'fs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
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
    const escalateFn = new lambda.Function(this, 'EscalateFn', {
      functionName: `${config.resourcePrefix}-tool-escalate`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'tools', 'escalate')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
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
    new agentcore.CfnGatewayTarget(this, 'EscalateTarget', {
      gatewayIdentifier: gateway.attrGatewayIdentifier,
      name: 'escalate_to_human',
      description:
        'Hand a matter to a human for review, with the reason. Use when the correct next action is not automatable -- e.g. a required document is overdue, a matter is internally inconsistent, or you cannot determine the right action.',
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

    const harnessRole = new iam.Role(this, 'HarnessRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'AgentCore Harness execution role -- invokes the model and the gateway.',
    });
    // Model access: build on Claude Sonnet 4.6 (ADR-001 -- build strong, eval
    // down). Invoke on the model + its cross-region inference profile.
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6-*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*anthropic.claude-sonnet-4-6-*`,
        ],
      }),
    );
    // Call the gateway at runtime. CONFIRMED action: the AgentCore IAM docs name
    // bedrock-agentcore:InvokeGateway as the runtime gateway-invocation action,
    // authorized on the Gateway ARN. Scoped to it rather than a wildcard.
    //   Residual (post-deploy) unknown: whether the managed Harness invokes the
    //   gateway via THIS execution role (needs the grant below) or brokers it
    //   service-side (grant harmless-but-unused). Either way InvokeGateway is
    //   the correct action, so this is proper scoping, not a guess. Cedar Policy
    //   actions (AuthorizeAction/PartiallyAuthorizeActions/GetPolicyEngine) are
    //   deliberately NOT here -- they attach to the GATEWAY role only when a
    //   Policy Engine exists, which the slice defers (ADR-006 guardrail split).
    harnessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [gateway.attrGatewayArn, `${gateway.attrGatewayArn}/*`],
      }),
    );

    this.harness = new agentcore.CfnHarness(this, 'Harness', {
      harnessName: `${config.resourcePrefix}-doc-chase-agent`,
      executionRoleArn: harnessRole.roleArn,
      model: {
        bedrockModelConfig: {
          modelId: 'anthropic.claude-sonnet-4-6-20260514-v1:0',
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
          name: 'gateway-tools',
          config: {
            agentCoreGateway: {
              gatewayArn: gateway.attrGatewayArn,
              outboundAuth: { awsIam: {} },
            },
          },
        },
      ],
      allowedTools: ['escalate_to_human'],
      maxIterations: 8,
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
