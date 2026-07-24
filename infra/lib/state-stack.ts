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
 * This is the source of truth the agent reasons over. The pipeline writes it;
 * the agent reads it and appends actions.
 *
 * SINGLE-TABLE DESIGN (introduced in the thin slice, Step 5):
 *
 *   PK = MATTER#<matterId>
 *     SK = META                 one row  -- matter metadata + rollup status
 *     SK = DOC#<docType>        one per required document -- status, dueDate,
 *                               sourceKey, extraction confidence
 *     SK = ACTION#<isoTs>       append-only agent/human action history
 *
 *   PK = TRIAGE
 *     SK = DOC#<documentId>     a document that arrived but could not be
 *                               associated with a matter (ADR-005). Carries the
 *                               extracted fields so a human can place it -- the
 *                               fields VERIFY, they do not auto-associate.
 *
 *   GSI1 (missing-docs-by-due-date, and the triage queue):
 *     GSI1PK = STATUS#<status>  e.g. STATUS#missing, STATUS#needs_triage
 *     GSI1SK = DUE#<dueDate>    (missing docs) | RECEIVED#<isoTs> (triage)
 *
 *   GSI1 lets the scheduled sweep answer "which documents are missing, ordered
 *   by due date" as a query rather than a table scan, and lets the readout count
 *   the triage queue with a single query. The sweep is not built in this slice --
 *   the GSI is included now, knowingly ahead of need, because adding it later
 *   would force a second table recreate (see ADR / build plan).
 */
export class StateStack extends cdk.Stack {
  public readonly matterTable: dynamodb.Table;

  /** GSI name, exported so Lambdas and scripts do not hard-code the string. */
  public static readonly GSI1_NAME = 'GSI1';

  constructor(scope: Construct, id: string, props: StateStackProps) {
    super(scope, id, props);

    const { config, dataKey } = props;

    this.matterTable = new dynamodb.Table(this, 'MatterTable', {
      tableName: `${config.resourcePrefix}-matters`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: dataKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: config.retainData ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    this.matterTable.addGlobalSecondaryIndex({
      indexName: StateStack.GSI1_NAME,
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

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
