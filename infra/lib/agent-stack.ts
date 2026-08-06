import * as path from 'path';
import * as fs from 'fs';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
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
    // --- Scheduled sweep Lambda ---------------------------------------------
    // Bundled at SYNTH time, without Docker (ADR-004 keeps synth container-free).
    // The asset is assembled by copying the `agent/` package -- decision core,
    // sweep logic, and the system prompt -- next to the handler, so that
    // `import agent.core.sweep` and the package-relative prompt path resolve
    // identically in the Lambda and on a laptop. `_bundle/` is generated and
    // gitignored; agent/system-prompt.md keeps exactly one source of truth.
    const sweepSrc = path.join(__dirname, '..', 'lambdas', 'sweep');
    const bundleDir = path.join(sweepSrc, '_bundle');
    const agentSrc = path.join(__dirname, '..', '..', 'agent');
    fs.rmSync(bundleDir, { recursive: true, force: true });
    fs.mkdirSync(path.join(bundleDir, 'agent'), { recursive: true });
    fs.copyFileSync(path.join(sweepSrc, 'index.py'), path.join(bundleDir, 'index.py'));
    fs.copyFileSync(path.join(agentSrc, '__init__.py'), path.join(bundleDir, 'agent', '__init__.py'));
    fs.copyFileSync(path.join(agentSrc, 'system-prompt.md'), path.join(bundleDir, 'agent', 'system-prompt.md'));
    fs.cpSync(path.join(agentSrc, 'core'), path.join(bundleDir, 'agent', 'core'), {
      recursive: true,
      filter: (src) => !src.includes('__pycache__'),
    });

    const sweepLogGroup = new logs.LogGroup(this, 'SweepFnLogGroup', {
      logGroupName: `/aws/lambda/${config.resourcePrefix}-sweep`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const sweepFn = new lambda.Function(this, 'SweepFn', {
      functionName: `${config.resourcePrefix}-sweep`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(bundleDir),
      // One Bedrock call per matter, serially, up to the cap. 15 min is the
      // ceiling; the MAX_MATTERS_PER_RUN cap is what actually bounds runtime.
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      logGroup: sweepLogGroup,
      environment: {
        MATTER_TABLE: matterTable.tableName,
        // Synth-time, from the gateway resource: no runtime lookup, no
        // bedrock-agentcore-control permission, no cold-start API call.
        GATEWAY_URL: gateway.attrGatewayUrl,
        GATEWAY_TOOL: 'escalate-to-human___escalate_to_human',
        // SAFE BY DEFAULT. Only the exact string "false" enables dispatch, so a
        // missing or malformed value dry-runs. Stage 2 flips this deliberately.
        DRY_RUN: 'true',
        // Stage A of capped-live: 5, per the approved staged rollout. Raised
        // only once live runs are boring. DRY_RUN stays 'true' here on purpose --
        // it is the DEFAULT, and the handler lets an explicit event payload
        // override it, so going live is a per-invocation decision rather than a
        // deployed state. The schedule sends dryRun:true in its own payload, so
        // it cannot go live as a side effect of anything done here.
        MAX_MATTERS_PER_RUN: '5',
        MAX_ESCALATIONS_PER_RUN: '10',
        LOOKAHEAD_DAYS: '7',
      },
    });

    // --- Sweep IAM: least privilege, and the absences are deliberate ---------
    // Query the matter table and GSI1 (candidate selection, the pre-filter, and
    // per-matter state reads).
    sweepFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [matterTable.tableArn, `${matterTable.tableArn}/index/GSI1`],
    }));
    // Write AUDIT# decision rows, including the error rows that keep a failing
    // matter visible.
    //
    // LIMITATION, recorded rather than papered over: DynamoDB IAM cannot scope
    // PutItem by SORT KEY prefix -- dynamodb:LeadingKeys conditions apply to the
    // partition key only. So "the sweep may write AUDIT# rows and nothing else"
    // is enforced by code, not by this role: an advisory guard, not a structural
    // one (docs/step-6-agent-design.md, probabilistic-guard principle).
    sweepFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'],
      resources: [matterTable.tableArn],
    }));
    // The table is CMK-encrypted, so the caller needs key access through DDB.
    dataKey.grant(sweepFn, 'kms:Decrypt', 'kms:GenerateDataKey');
    // Invoke the production model. Sonnet 4.6 is included because it is the
    // documented fallback (ADR-001) and switching to it is an env-var change.
    sweepFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5*`,
        `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6*`,
        `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0`,
        `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-6`,
      ],
    }));
    // Dispatch through the Gateway -- the governed surface that holds the
    // credentials to invoke the tool Lambda and logs every call.
    sweepFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'],
      resources: [gateway.attrGatewayArn, `${gateway.attrGatewayArn}/*`],
    }));
    // DELIBERATELY ABSENT -- asserted by scripts/verify-sweep-iam.py against the
    // synthesized template, so the design doc and the deployed role cannot drift:
    //   lambda:InvokeFunction        the GATEWAY invokes the escalate Lambda
    //   sns:Publish                  the escalate Lambda notifies, never the sweep
    //   dynamodb:UpdateItem/DeleteItem/BatchWriteItem  never mutates matter state
    //   dynamodb:Scan                candidate selection is Query-only
    //   bedrock-agentcore-control:*  eliminated by the GATEWAY_URL env var

    // --- Day 2: the schedule ------------------------------------------------
    // The first thing in this system that triggers itself with no human in the
    // loop. It runs with DRY_RUN=true (the function's own env default), so a
    // firing decides and records and dispatches nothing.
    //
    // EventBridge SCHEDULER, not an events.Rule, specifically for the timezone.
    // A Rule's cron is UTC-only, so "07:00 local" would have to be written as a
    // fixed UTC hour and would silently drift an hour across the DST boundary --
    // 07:00 EDT is 11:00 UTC, 07:00 EST is 12:00 UTC. Scheduler takes an IANA
    // zone and holds 07:00 local year-round. Given this project has already lost
    // time to a UTC/local date confusion, the schedule states its intent rather
    // than encoding an offset that is only correct half the year.
    const schedulerRole = new iam.Role(this, 'SweepScheduleRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      description: 'EventBridge Scheduler role -- invokes the sweep Lambda on the daily schedule.',
    });
    sweepFn.grantInvoke(schedulerRole);

    new scheduler.CfnSchedule(this, 'SweepDailySchedule', {
      name: `${config.resourcePrefix}-sweep-daily`,
      description: 'Daily document-chase sweep (Day 2: cron-driven dry run).',
      state: 'ENABLED',
      flexibleTimeWindow: { mode: 'OFF' },  // fire at the stated minute, not within a window
      scheduleExpression: 'cron(0 7 * * ? *)',
      scheduleExpressionTimezone: 'America/New_York',
      target: {
        arn: sweepFn.functionArn,
        roleArn: schedulerRole.roleArn,
        // The payload is the trigger's fingerprint. `invokedBy` is what lets a
        // reader tell a scheduled firing from a hand-invoke after the fact --
        // a RequestId and a timestamp cannot. dryRun is stated explicitly here
        // as well as defaulted in the function env: two independent statements
        // of the same intent, so neither alone is load-bearing.
        // NO dryRun here on purpose. The function's DRY_RUN env is the single
        // lever for the unattended path; a payload copy would be a second place
        // the same intent is stated, able to diverge and silently win.
        // invokedBy stays -- it is what distinguishes a scheduled firing from a
        // hand-invoke after the fact.
        input: JSON.stringify({ invokedBy: 'eventbridge-scheduler' }),
        retryPolicy: { maximumRetryAttempts: 0 },  // a missed day is better than a double sweep
      },
    });

    new cdk.CfnOutput(this, 'SweepFnName', { value: sweepFn.functionName });

    // --- Operations: alarms + ops topic -------------------------------------
    // SEPARATE topic from escalations on purpose. An ops signal must never look
    // like a client-facing escalation in an inbox, and the two have different
    // audiences and different urgency.
    const opsTopic = new sns.Topic(this, 'SweepOpsTopic', {
      topicName: `${config.resourcePrefix}-sweep-ops`,
      masterKey: dataKey,
    });
    new sns.Subscription(this, 'SweepOpsEmailSub', {
      topic: opsTopic,
      protocol: sns.SubscriptionProtocol.EMAIL,
      endpoint: config.messaging.testRecipient,
    });
    const opsAction = new cwActions.SnsAction(opsTopic);

    // 1. DID SOMETHING NOTABLE. Silence is the normal case; a daily "nothing
    //    happened" mail would only train the reader to ignore it.
    const notableMetric = sweepLogGroup.addMetricFilter('SweepNotableFilter', {
      filterPattern: logs.FilterPattern.literal('"SWEEP_NOTABLE"'),
      metricNamespace: metricNamespace,
      metricName: 'SweepNotable',
      metricValue: '1',
      defaultValue: 0,
    }).metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) });

    new cloudwatch.Alarm(this, 'SweepNotableAlarm', {
      alarmName: `${config.resourcePrefix}-sweep-notable`,
      alarmDescription: 'The sweep escalated, errored, or tripped the escalation valve.',
      metric: notableMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(opsAction);

    // 2. DEAD-MAN'S SWITCH -- the one that matters most. Without it, "no alarm
    //    email" is ambiguous between "nothing to report" and "the sweep has been
    //    dead for a week", and the second failure is silent by construction.
    //    Dimension is ScheduleGroup (what AWS/Scheduler actually publishes);
    //    with one schedule in the group that is exact, and would need revisiting
    //    if a second schedule joined it.
    // WATCHES COMPLETION, NOT ATTEMPTS -- corrected after the kill-switch test.
    //
    // The first version watched AWS/Scheduler InvocationAttemptCount. Arming the
    // kill switch proved that wrong: with reserved concurrency at 0 the Scheduler
    // still ATTEMPTED (InvocationAttemptCount went to 1) while the function never
    // ran -- and, tellingly, Lambda emitted no Throttles and Scheduler emitted no
    // TargetErrorCount or InvocationDroppedCount. The block is entirely silent.
    // A dead-man's switch on attempts would therefore have reported a paused,
    // throttled, or broken sweep as healthy, which is the precise failure it
    // exists to catch.
    //
    // This metric comes from the sweep's own completion log line, so it can only
    // increment if the function actually ran to the end.
    sweepLogGroup.addMetricFilter('SweepCompletedFilter', {
      filterPattern: logs.FilterPattern.literal('"sweep summary"'),
      metricNamespace: metricNamespace,
      metricName: 'SweepCompleted',
      metricValue: '1',
      defaultValue: 0,
    });
    const sweepRan = new cloudwatch.Metric({
      namespace: metricNamespace,
      metricName: 'SweepCompleted',
      statistic: 'Sum',
      period: cdk.Duration.hours(24),
    });
    new cloudwatch.Alarm(this, 'SweepDidNotRunAlarm', {
      alarmName: `${config.resourcePrefix}-sweep-did-not-run`,
      alarmDescription: 'The daily sweep has not COMPLETED in 24h (throttled, paused, broken, or never fired).',
      metric: sweepRan,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,  // no data == did not run
    }).addAlarmAction(opsAction);

    // 3. The Lambda blew up.
    new cloudwatch.Alarm(this, 'SweepErrorsAlarm', {
      alarmName: `${config.resourcePrefix}-sweep-errors`,
      alarmDescription: 'The sweep Lambda raised an unhandled error.',
      metric: sweepFn.metricErrors({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(opsAction);

    new cdk.CfnOutput(this, 'SweepOpsTopicArn', { value: opsTopic.topicArn });

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
