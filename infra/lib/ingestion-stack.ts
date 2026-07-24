import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface IngestionStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
}

/**
 * The landing zone. Every document enters the system here and nowhere else --
 * inbound SES email attachments and portal uploads both write to this bucket.
 *
 * Versioned because a regulated back office needs to prove what it received and
 * when, even if a later write overwrote it.
 */
export class IngestionStack extends cdk.Stack {
  public readonly rawBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, props);

    const { config, dataKey } = props;

    this.rawBucket = new s3.Bucket(this, 'RawBucket', {
      // Account id keeps the name globally unique without hard-coding it in the repo.
      bucketName: `${config.resourcePrefix}-raw-${this.account}`,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: dataKey,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      // Emit S3 events to the default EventBridge bus. The submit Lambda (in the
      // understanding stack) subscribes via an EventBridge rule rather than a
      // bucket notification, so the producer (this bucket) and the consumer stay
      // in separate stacks without a cross-stack notification dependency.
      eventBridgeEnabled: true,
      removalPolicy: config.retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !config.retainData,
    });

    // TODO(ingestion step): SES inbound receipt rule writing to this bucket
    // (blocked on the ADR-005 domain track), a lifecycle rule moving raw objects
    // to Glacier after the retention window, and a separate server-access-log
    // bucket. The EventBridge notification that kicks off BDA is now wired (see
    // eventBridgeEnabled above + the understanding stack).

    new cdk.CfnOutput(this, 'RawBucketName', {
      value: this.rawBucket.bucketName,
      description: 'S3 bucket where inbound documents land before processing.',
    });

    new cdk.CfnOutput(this, 'RawBucketArn', {
      value: this.rawBucket.bucketArn,
      description: 'ARN of the raw document bucket.',
    });
  }
}
