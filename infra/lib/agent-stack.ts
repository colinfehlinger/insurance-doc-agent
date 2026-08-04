import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
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
 * separate sub-project (ADR-006). Tools attach through a Gateway; every tool
 * call is logged by the Gateway. The managed Harness was retired (ADR-007 --
 * its runtime did not inject tools); orchestration is a client-side loop
 * (scripts/agent-loop.py). Cedar Policy and the other four tools are reserved
 * for later passes (docs/step-6-agent-design.md).
 */
export class AgentStack extends cdk.Stack {
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
    // Explicit, stably-named subscription resource (not the auto-named
    // addSubscription form) so its logical id survives refactors and it is a
    // durable stack resource rather than a manual `aws sns subscribe`. Email
    // subscriptions are created PENDING and require a one-time human confirmation
    // click; an unconfirmed pending sub is auto-deleted by SNS after ~3 days --
    // which is how the topic went bare after the Step-6 destroy/recreate churn.
    // A no-click durable channel (SNS -> SQS, or SNS -> Lambda -> SES on a
    // verified domain) is the later upgrade; for now email is the escalation
    // channel.
    new sns.Subscription(this, 'EscalationEmailSub', {
      topic: this.escalationTopic,
      protocol: sns.SubscriptionProtocol.EMAIL,
      endpoint: config.messaging.testRecipient,
    });

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

    // --- Harness: RETIRED (ADR-007) --------------------------------------
    // The managed AgentCore Harness runtime did not inject tools into the
    // model request (gateway, inline, and built-in tools all absent from the
    // literal ConverseStream request). Orchestration moved to a client-side
    // loop (scripts/agent-loop.py): direct Bedrock Converse + a toolConfig it
    // builds from the escalate schema, dispatching tool_use through THIS
    // Gateway (SigV4/MCP). The Gateway, its target, the tool Lambda, and SNS
    // all stay -- only the Harness (and its execution role) are gone.
    // --- SSM contract + messaging config ------------------------------------
    // Replaces the probe's runtime-arn placeholder with the real harness ARN.

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

    new cdk.CfnOutput(this, 'GatewayArn', { value: gateway.attrGatewayArn });
    new cdk.CfnOutput(this, 'EscalationTopicArn', { value: this.escalationTopic.topicArn });
    new cdk.CfnOutput(this, 'EscalateFnName', { value: escalateFn.functionName });
  }
}
