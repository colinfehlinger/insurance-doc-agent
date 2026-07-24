import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface AgentStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
  readonly matterTable: dynamodb.ITable;
}

/**
 * STUB -- placeholder for the only part of the system that is allowed to
 * exercise judgment.
 *
 * The agent does not classify documents and does not extract fields. It reads
 * a matter's state and decides what should happen next: remind, wait, escalate,
 * or flag something odd. Everything it can actually do is a tool call, so the
 * blast radius is the tool list rather than the model.
 *
 * For now it publishes the SSM parameter that the invoker will read.
 */
export class AgentStack extends cdk.Stack {
  /** SSM parameter that will hold the AgentCore runtime ARN once it exists. */
  public readonly agentRuntimeArnParam: ssm.StringParameter;

  /** Threaded through now so the agent step does not have to reshape the app. */
  public readonly dataKey: kms.IKey;
  public readonly matterTable: dynamodb.ITable;

  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);

    const { config } = props;
    this.dataKey = props.dataKey;
    this.matterTable = props.matterTable;

    this.agentRuntimeArnParam = new ssm.StringParameter(this, 'AgentRuntimeArnParam', {
      parameterName: `${config.ssmPrefix}/agent/runtime-arn`,
      stringValue: 'PLACEHOLDER-NOT-YET-PROVISIONED',
      description: 'ARN of the Bedrock AgentCore runtime that decides the next action per matter.',
    });

    // Messaging config for the agent's send_reminder tool. Kept as SSM parameters
    // (sourced from config.ts), never hard-coded in Lambda/agent source, so the
    // recipient and sender change per stage without a code change.
    //   NOTE: senderAddress is not yet a verified SES identity. SES production
    //   access is enabled on this account, but a From address still needs the
    //   address or its domain verified before it can send. As of 2026-07-22 the
    //   only verified identity is test-recipient@example.com; fehlingerops.com is
    //   not registered (ADR-005 domain track). Point senderAddress at the gmail
    //   to actually send before the domain exists.
    new ssm.StringParameter(this, 'ReminderRecipientParam', {
      parameterName: `${config.ssmPrefix}/messaging/test-recipient`,
      stringValue: config.messaging.testRecipient,
      description: 'Where send_reminder delivers in this stage (dev: single test recipient).',
    });

    new ssm.StringParameter(this, 'ReminderSenderParam', {
      parameterName: `${config.ssmPrefix}/messaging/sender-address`,
      stringValue: config.messaging.senderAddress,
      description: 'From address for send_reminder. Must be a verified SES identity before it can send.',
    });

    // TODO(AgentCore step): stand up the real runtime. Confirmed surface as of
    // July 2026 -- re-check before writing it, this service moves fast:
    //
    //   aws-cdk-lib/aws-bedrockagentcore (stable L1):
    //     CfnRuntime, CfnRuntimeEndpoint, CfnMemory, CfnGateway,
    //     CfnGatewayTarget, CfnBrowserCustom, CfnCodeInterpreterCustom,
    //     CfnWorkloadIdentity
    //   @aws-cdk/aws-bedrock-agentcore-alpha (alpha L2, version-locked to the
    //     aws-cdk-lib release): Runtime, AgentRuntimeArtifact.fromAsset()
    //
    // Deployment ordering gotcha: a CfnRuntime will not resolve unless a valid
    // container image already exists at the referenced tag. So the sequence is
    // CodeBuild builds and pushes the image -> a Custom Resource waits for the
    // tag -> the runtime is created. The alpha L2's
    // AgentRuntimeArtifact.fromAsset() handles this, at the cost of depending
    // on an alpha module.
    //
    // Also lands in that step:
    //  - CfnMemory for per-matter and per-counterparty context, so a second
    //    reminder does not read like the first.
    //  - CfnGateway + CfnGatewayTarget exposing the five tools
    //    (send_reminder, schedule_followup, escalate_to_human, update_matter,
    //    flag_anomaly). See ../../agent/tools/README.md.
    //  - Policy guardrails (CfnPolicy / CfnPolicyEngine, GA March 2026) for the
    //    hard limits: never email an insured directly, never send more than N
    //    reminders, always escalate past the due date.
    //  - Observability wired to the decision audit trail -- for this domain the
    //    log of why the agent chose an action is a deliverable, not a debug aid.
    //  - Least-privilege execution role: read matter state, write action
    //    history, decrypt with the shared CMK, and nothing else.

    new cdk.CfnOutput(this, 'AgentRuntimeArnParamName', {
      value: this.agentRuntimeArnParam.parameterName,
      description: 'SSM parameter that will hold the AgentCore runtime ARN.',
    });
  }
}
