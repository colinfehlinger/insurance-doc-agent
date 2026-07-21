import * as cdk from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { IdaStackPropsBase } from './config';

export interface UnderstandingStackProps extends cdk.StackProps, IdaStackPropsBase {
  readonly dataKey: kms.IKey;
  readonly rawBucket: s3.IBucket;
}

/**
 * STUB -- placeholder for the classification and extraction half of the
 * deterministic pipeline.
 *
 * When this is real it will hold a Bedrock Data Automation project (blueprints
 * per document type), the Lambda that invokes it on new objects in the raw
 * bucket, and the writer that turns extracted fields into matter state. None of
 * that is agentic on purpose: extraction has to be reproducible and auditable,
 * so the model runs inside a fixed pipeline with confidence thresholds rather
 * than inside an agent loop.
 *
 * For now it publishes the SSM parameter that later stacks will read, so the
 * contract between steps exists before the implementation does.
 */
export class UnderstandingStack extends cdk.Stack {
  /** SSM parameter that will hold the BDA project ARN once it exists. */
  public readonly bdaProjectArnParam: ssm.StringParameter;

  /** Threaded through now so the pipeline step does not have to reshape the app. */
  public readonly dataKey: kms.IKey;
  public readonly rawBucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: UnderstandingStackProps) {
    super(scope, id, props);

    const { config } = props;
    this.dataKey = props.dataKey;
    this.rawBucket = props.rawBucket;

    this.bdaProjectArnParam = new ssm.StringParameter(this, 'BdaProjectArnParam', {
      parameterName: `${config.ssmPrefix}/bda/project-arn`,
      stringValue: 'PLACEHOLDER-NOT-YET-PROVISIONED',
      description:
        'ARN of the Bedrock Data Automation project used to classify and extract benefit documents.',
    });

    // TODO(IDP step): replace this placeholder by composing the CDK-native
    // accelerator -- @cdklabs/genai-idp plus @cdklabs/genai-idp-bda-processor
    // (Pattern 1 = BDA) -- rather than hand-rolling the pipeline. Keep the
    // matter-state model and the agent in this repo; reuse only the ingestion
    // and extraction plumbing.
    //
    // Notes carried forward for that step:
    //  - BDA is invoked through the US cross-region inference profile
    //    arn:aws:bedrock:us-east-1:<account>:data-automation-profile/us.data-automation-v1.
    //    Objects stay in us-east-1, but inference may traverse us-east-1,
    //    us-east-2, us-west-1 and us-west-2. This must be written down in the
    //    data-residency section of the audit docs before any real PHI/PII lands.
    //  - Confidence scores below the per-field threshold route to human review
    //    instead of writing straight to matter state.

    new cdk.CfnOutput(this, 'BdaProjectArnParamName', {
      value: this.bdaProjectArnParam.parameterName,
      description: 'SSM parameter that will hold the BDA project ARN.',
    });
  }
}
