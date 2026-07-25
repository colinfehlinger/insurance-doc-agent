import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface UnderstandingStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
  readonly rawBucket: s3.IBucket;
  readonly matterTable: dynamodb.ITable;
}

/**
 * The classification and extraction half of the fixed pipeline.
 *
 * There is no agentic loop here, on purpose: extraction has to be auditable, so
 * the model (Bedrock Data Automation) runs inside a fixed code path with a
 * human-review confidence threshold -- never inside an agent loop. BDA is itself
 * GenAI-powered, which is why the claim is "fixed and auditable" rather than
 * "deterministic" (docs/decisions/ADR-002-bda-vs-textract.md).
 *
 * Shape (Step 5 thin slice):
 *
 *   raw bucket --(S3 EventBridge "Object Created")--> Submit Lambda
 *     Submit: resolve_matter(key) [ADR-005], then InvokeDataAutomationAsync with
 *             a deterministic clientToken, our CMK, and eventBridgeEnabled. No
 *             polling.
 *   BDA --(EventBridge "Job Succeeded/Failed*")--> Mapper Lambda
 *     Mapper: read extracted fields + confidence, resolve_matter again, write
 *             the matter's DOC# row (>= threshold = received, else in-review) or
 *             a TRIAGE row if unassociated.
 *
 * We call the BDA APIs directly rather than adopt the accelerator (ADR-004); the
 * retry/backoff/error-classification patterns and metric names are lifted from
 * its source (docs/bda-orchestration-reference.md).
 */
export class UnderstandingStack extends cdk.Stack {
  public readonly bdaProjectArnParam: ssm.StringParameter;
  public readonly bdaProject: bedrock.CfnDataAutomationProject;

  constructor(scope: Construct, id: string, props: UnderstandingStackProps) {
    super(scope, id, props);

    const { config, dataKey, rawBucket, matterTable } = props;
    const metricNamespace = `Ida/${config.stage}/Understanding`;
    const outputPrefix = 'bda-output';

    // The US cross-region inference profile BDA runs through. Objects stay in
    // us-east-1 but inference may traverse us-east-1/2 and us-west-1/2 -- recorded
    // in the data-residency section of docs/architecture.md.
    const bdaProfileArn = `arn:aws:bedrock:${this.region}:${this.account}:data-automation-profile/us.data-automation-v1`;

    // --- BDA project + census blueprint --------------------------------------
    // One blueprint for this slice: the group-benefits census, chosen over the
    // SBC because it is matter-central and carries the identifying fields
    // (group number, employer) that make correlation real. `schema` is untyped
    // (any) so it is not synth-validated; BDA validates it at deploy.
    const censusSchema = JSON.parse(
      require('fs').readFileSync(path.join(__dirname, '..', 'bda', 'census-blueprint.json'), 'utf-8'),
    );

    const censusBlueprint = new bedrock.CfnBlueprint(this, 'CensusBlueprint', {
      blueprintName: `${config.resourcePrefix}-census`,
      type: 'DOCUMENT',
      schema: censusSchema,
      kmsKeyId: dataKey.keyArn,
    });

    this.bdaProject = new bedrock.CfnDataAutomationProject(this, 'BdaProject', {
      projectName: `${config.resourcePrefix}-idp`,
      projectDescription: 'Classifies and extracts group-benefits documents for the Document-Chase Agent.',
      kmsKeyId: dataKey.keyArn,
      customOutputConfiguration: {
        blueprints: [{ blueprintArn: censusBlueprint.attrBlueprintArn }],
      },
      // REQUIRED by the CreateDataAutomationProject API even though the L1 marks
      // it optional -- verified via the CLI (create-data-automation-project
      // rejects a project with no standardOutputConfiguration). Omitting it would
      // have been a second rollback after the blueprint one. Minimal document
      // extraction; the custom blueprint above drives the actual field pull.
      standardOutputConfiguration: {
        document: {
          extraction: {
            granularity: { types: ['DOCUMENT'] },
            boundingBox: { state: 'DISABLED' },
          },
          generativeField: { state: 'DISABLED' },
        },
      },
    });

    // --- Submit Lambda -------------------------------------------------------
    const submitFn = new lambda.Function(this, 'SubmitFn', {
      functionName: `${config.resourcePrefix}-bda-submit`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      // Plain asset, no bundling -- boto3 is in the runtime, so no Docker is
      // needed (the property ADR-004 preserved by not adopting the accelerator).
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'submit')),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        BDA_PROJECT_ARN: this.bdaProject.attrProjectArn,
        BDA_PROFILE_ARN: bdaProfileArn,
        OUTPUT_BUCKET: rawBucket.bucketName,
        OUTPUT_PREFIX: outputPrefix,
        KMS_KEY_ARN: dataKey.keyArn,
        METRIC_NAMESPACE: metricNamespace,
      },
    });

    // BDA runs as the caller's role (CloudTrail shows invokedBy bedrock.amazonaws.com
    // assuming this role), so this role needs read on the input and write on the
    // output prefix, plus the KMS permissions below.
    rawBucket.grantReadWrite(submitFn);

    // Customer-managed-key access for BDA, per the documented grant flow
    // (docs.aws.amazon.com/bedrock/latest/userguide/encryption-bda.html): when
    // encryptionConfiguration.kmsKeyId is a CMK, BDA -- acting as this role --
    // calls kms:CreateGrant, and uses DescribeKey/GenerateDataKey/Decrypt. The
    // ViaService condition scopes these to Bedrock, matching the AWS example.
    //
    // NOTE: this lives here, on the caller's role, NOT in SharedStack. The shared
    // key's DEFAULT key policy already delegates to IAM via the root-account
    // statement, so an IAM grant on this role is sufficient and idiomatic. Adding
    // a key-policy statement in SharedStack that named this role would create a
    // circular dependency (Shared -> role, Understanding -> key). grantReadWrite
    // already covers Decrypt/GenerateDataKey; this adds CreateGrant + DescribeKey.
    dataKey.grant(submitFn, 'kms:CreateGrant', 'kms:DescribeKey');
    submitFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeDataAutomationAsync'],
        resources: [
          this.bdaProject.attrProjectArn,
          // The CRIS profile must be permitted in every region it can route to.
          `arn:aws:bedrock:us-east-1:${this.account}:data-automation-profile/us.data-automation-v1`,
          `arn:aws:bedrock:us-east-2:${this.account}:data-automation-profile/us.data-automation-v1`,
          `arn:aws:bedrock:us-west-1:${this.account}:data-automation-profile/us.data-automation-v1`,
          `arn:aws:bedrock:us-west-2:${this.account}:data-automation-profile/us.data-automation-v1`,
        ],
      }),
    );
    submitFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['cloudwatch:PutMetricData'], resources: ['*'] }),
    );

    // --- Mapper Lambda -------------------------------------------------------
    const mapperFn = new lambda.Function(this, 'MapperFn', {
      functionName: `${config.resourcePrefix}-bda-mapper`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambdas', 'mapper')),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        MATTER_TABLE: matterTable.tableName,
        CONFIDENCE_THRESHOLD: String(config.extractionConfidenceThreshold),
        METRIC_NAMESPACE: metricNamespace,
        DOC_TYPE: 'census',
      },
    });

    // Mapper reads the BDA output (custom_output/result.json) from the raw
    // bucket and decrypts it with the CMK, then writes matter state. It no longer
    // calls bedrock:GetDataAutomationStatus -- the completion event carries the
    // output location directly (confirmed 2026-07-25), so that permission is
    // dropped. grantRead covers reading the CMK-encrypted output.
    rawBucket.grantRead(mapperFn);
    matterTable.grantReadWriteData(mapperFn);
    dataKey.grantEncryptDecrypt(mapperFn);
    mapperFn.addToRolePolicy(
      new iam.PolicyStatement({ actions: ['cloudwatch:PutMetricData'], resources: ['*'] }),
    );

    // --- Event wiring --------------------------------------------------------
    // S3 Object Created -> Submit. Match the ingestion prefixes only, so BDA's
    // own writes under bda-output/ never re-trigger the pipeline. `unassociated/`
    // is included so the triage path is exercisable end to end.
    new events.Rule(this, 'ObjectCreatedRule', {
      ruleName: `${config.resourcePrefix}-object-created`,
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [rawBucket.bucketName] },
          object: { key: [{ prefix: 'matters/' }, { prefix: 'unassociated/' }] },
        },
      },
      targets: [new targets.LambdaFunction(submitFn)],
    });

    // BDA completion -> Mapper. Three of the four detail-types; "Job Created" is
    // deliberately not subscribed.
    new events.Rule(this, 'BdaCompletionRule', {
      ruleName: `${config.resourcePrefix}-bda-completion`,
      eventPattern: {
        source: ['aws.bedrock'],
        detailType: [
          'Bedrock Data Automation Job Succeeded',
          'Bedrock Data Automation Job Failed With Client Error',
          'Bedrock Data Automation Job Failed With Service Error',
        ],
      },
      targets: [new targets.LambdaFunction(mapperFn)],
    });

    // --- SSM contract --------------------------------------------------------
    this.bdaProjectArnParam = new ssm.StringParameter(this, 'BdaProjectArnParam', {
      parameterName: `${config.ssmPrefix}/bda/project-arn`,
      stringValue: this.bdaProject.attrProjectArn,
      description: 'ARN of the Bedrock Data Automation project used to classify and extract benefit documents.',
    });

    new ssm.StringParameter(this, 'ConfidenceThresholdParam', {
      parameterName: `${config.ssmPrefix}/bda/confidence-threshold`,
      stringValue: String(config.extractionConfidenceThreshold),
      description: 'Per-field extraction confidence below which a document routes to human review (ADR-002).',
    });

    new cdk.CfnOutput(this, 'BdaProjectArn', { value: this.bdaProject.attrProjectArn });
    new cdk.CfnOutput(this, 'SubmitFnName', { value: submitFn.functionName });
    new cdk.CfnOutput(this, 'MapperFnName', { value: mapperFn.functionName });
  }
}
