import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface StateStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
}

/**
 * Matter state: for each matter, which documents are required, which have been
 * received, which are still missing, when they are due, and what has already
 * been done about it.
 *
 * This is the source of truth the agent reasons over. The pipeline writes it
 * deterministically; the agent reads it and appends actions.
 */
export class StateStack extends cdk.Stack {
  public readonly matterTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: StateStackProps) {
    super(scope, id, props);

    const { config, dataKey } = props;

    this.matterTable = new dynamodb.Table(this, 'MatterTable', {
      tableName: `${config.resourcePrefix}-matters`,
      partitionKey: { name: 'matterId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: config.retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // TODO(thin-slice step): replace this partition-key-only table with the real
    // single-table design -- a sort key (DOC#<type>, ACTION#<ts>, META) plus a GSI
    // for "missing documents by due date" so the scheduled sweep is a query rather
    // than a scan. That change recreates the table, which is why it is deferred
    // until there is a schema worth committing to. Acceptable in dev; prod would
    // need a migration.

    new cdk.CfnOutput(this, 'MatterTableName', {
      value: this.matterTable.tableName,
      description: 'DynamoDB table holding per-matter document state.',
    });

    new cdk.CfnOutput(this, 'MatterTableArn', {
      value: this.matterTable.tableArn,
      description: 'ARN of the matter state table.',
    });
  }
}
