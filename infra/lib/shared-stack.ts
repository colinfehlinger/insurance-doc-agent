import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface SharedStackProps extends cdk.StackProps, IdaStackPropsBase {}

/**
 * One customer-managed KMS key for every at-rest data store in the stage.
 *
 * A single CMK (rather than per-service keys) keeps the compliance story simple:
 * one key to audit, one key to rotate, one key to revoke on offboarding. If a
 * tenant ever needs its own key, this is the seam where that changes.
 */
export class SharedStack extends cdk.Stack {
  /** CMK used by the raw bucket, the matter table, and later BDA + AgentCore. */
  public readonly dataKey: kms.Key;

  constructor(scope: Construct, id: string, props: SharedStackProps) {
    super(scope, id, props);

    const { config } = props;

    this.dataKey = new kms.Key(this, 'DataKey', {
      alias: `alias/${config.resourcePrefix}-data`,
      description: `Customer-managed key for insurance-doc-agent ${config.stage} data at rest.`,
      enableKeyRotation: true,
      removalPolicy: config.retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      // Dev uses the 7-day minimum so a destroyed stage can be rebuilt quickly.
      pendingWindow: cdk.Duration.days(config.retainData ? 30 : 7),
    });

    // CloudWatch Alarms -> CMK-encrypted SNS.
    //
    // An IAM-principal caller (a Lambda with kms grants) can publish to a
    // CMK-encrypted topic because its ROLE carries the key permissions. A SERVICE
    // principal cannot: CloudWatch Alarms calls KMS as cloudwatch.amazonaws.com,
    // which the default root-only key policy does not cover. Without this the
    // publish fails and the alarm notification is lost -- CloudWatch records
    // "Failed to execute action <topic-arn>" in the alarm's Action history while
    // the alarm itself still transitions to ALARM perfectly normally.
    //
    // Found on 2026-08-07, the first live unattended sweep: ida-dev-sweep-notable
    // reached ALARM and published NOTHING (topic NumberOfMessagesPublished = 0),
    // so no ops email was ever sent. ida-dev-escalations on the SAME key was
    // unaffected, because the escalate Lambda publishes with its own role.
    //
    // Scoped so this grants nothing beyond that path: SNS only, this account
    // only.
    this.dataKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudWatchAlarmsToPublishToEncryptedSns',
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['kms:GenerateDataKey*', 'kms:Decrypt'],
        resources: ['*'], // a key policy's Resource is the key it lives on
        conditions: {
          StringEquals: {
            'kms:ViaService': `sns.${this.region}.amazonaws.com`,
            'aws:SourceAccount': this.account,
          },
        },
      }),
    );

    // TODO(compliance step): tighten the key policy beyond the default
    // root-account grant, and add explicit grants for the Bedrock Data
    // Automation and AgentCore service principals once those stacks are real.

    new cdk.CfnOutput(this, 'DataKeyArn', {
      value: this.dataKey.keyArn,
      description: 'ARN of the shared customer-managed KMS key.',
    });

    new cdk.CfnOutput(this, 'DataKeyAlias', {
      value: `alias/${config.resourcePrefix}-data`,
      description: 'Alias of the shared customer-managed KMS key.',
    });
  }
}
