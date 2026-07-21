import * as cdk from 'aws-cdk-lib';
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
