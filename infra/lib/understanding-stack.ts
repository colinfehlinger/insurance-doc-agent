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
 * fixed pipeline.
 *
 * When this is real it will hold a Bedrock Data Automation project (blueprints
 * per document type), the Lambda that invokes it on new objects in the raw
 * bucket, and the writer that turns extracted fields into matter state. There is
 * no agentic loop here, on purpose: extraction has to be auditable, so the model
 * runs inside a fixed code path with confidence thresholds and a human-review
 * threshold below them -- never inside an agent loop. (BDA is itself
 * GenAI-powered, which is why the claim is "fixed and auditable" rather than
 * "deterministic" -- see docs/decisions/ADR-002-bda-vs-textract.md.)
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

    // TODO(thin-slice step): build this directly. The Step 4 triage decided
    // AGAINST adopting the CDK-native accelerator (@cdklabs/genai-idp +
    // @cdklabs/genai-idp-bda-processor) -- see ADR-004 and
    // docs/idp-accelerator-triage.md. Short version: it composes fine (it takes
    // our bucket and CMK as props) but it is a 59-Lambda product carrying five
    // ALPHA CDK peer dependencies, and it would take infra/ from 6 direct
    // dependencies to 14+ and make Docker a prerequisite for `cdk synth` -- to
    // use roughly three Lambdas' worth of behaviour.
    //
    // What to build instead -- EVENT-DRIVEN, not a poll loop. BDA supports
    // EventBridge completion notifications natively, so there is no polling to
    // write or to get wrong:
    //  - Submit Lambda: S3 event -> InvokeDataAutomationAsync with
    //    clientToken (idempotency; duplicate S3 events are real) and
    //    notificationConfiguration.eventBridgeConfiguration.eventBridgeEnabled
    //    = true. Pass our CMK via encryptionConfiguration.kmsKeyId.
    //  - EventBridge rule on source 'aws.bedrock', detail-types:
    //      Bedrock Data Automation Job Succeeded
    //      Bedrock Data Automation Job Failed With Client Error   <- non-retryable
    //      Bedrock Data Automation Job Failed With Service Error  <- retryable
    //    The client/service split IS our error classification -- take it from
    //    the event type rather than inferring it from an exception.
    //  - Completion Lambda: parse the result -> hand classified type + fields +
    //    per-field confidence to a matter-state mapper.
    //  - One Data Automation Project + one blueprint per document class, created
    //    by us (the accelerator would have generated these at synth time).
    //
    // Step 5 PREREQUISITE, before writing any BDA call (see ADR-004): extract
    // the accelerator's SUBMIT-SIDE throttle/retry/backoff and error
    // classification from bda-invoke / bda-completion / bda-processresults
    // (Apache-2.0; `npm pack` and read, no dependency added). Their polling
    // machinery is moot -- EventBridge replaces it. Emit their metric set from
    // day one (Throttles, RetrySuccess, MaxRetriesExceeded, NonRetryableErrors,
    // JobsSucceeded/Failed) or ADR-004's reversal trigger is unmeasurable.
    //
    // Notes carried forward:
    //  - BDA is invoked through the US cross-region inference profile
    //    arn:aws:bedrock:us-east-1:<account>:data-automation-profile/us.data-automation-v1.
    //    Objects stay in us-east-1, but inference may traverse us-east-1,
    //    us-east-2, us-west-1 and us-west-2. This must be written down in the
    //    data-residency section of the audit docs before any real PHI/PII lands.
    //  - Confidence scores below the per-field threshold route to human review
    //    instead of writing straight to matter state.
    //  - Expose exactly one thing to the rest of the system: classified type +
    //    extracted fields + per-field confidence. No BDA-shaped types reach the
    //    matter-state writer. That narrow seam is what keeps BOTH the ADR-002
    //    processor swap (BDA -> Textract) and the ADR-004 reversal (direct ->
    //    accelerator) cheap.
    //  - Instrument the BDA calls from day one (throttles, retries, retries
    //    exhausted, non-retryable errors). ADR-004's primary reversal trigger is
    //    "our orchestration proves unreliable" -- unmeasurable without metrics.
    //  - BOUNDARY GUARD, because the type system gives none. The accelerator's
    //    `ITrackingTable extends ITable` adds NO members, so passing
    //    ida-dev-matters where a tracking table is expected would COMPILE
    //    CLEANLY and then write processing-status items into the product's
    //    system of record. Processing status and matter state stay in separate
    //    tables with separate lifecycles: processing status is transient (365-day
    //    default retention in their model), matter state is indefinite -- it IS
    //    the record. If ADR-004 is ever reversed, the accelerator gets its own
    //    tracking table and we let it.
    //      A document can process perfectly and leave a matter incomplete.
    //    The converse holds too: a matter can be complete while a processing job
    //    failed and was retried. The two never model each other.
    //  - CORRELATION (ADR-005) -- decide the mechanism BEFORE the ingestion
    //    design, not during it. Nothing on a document says which matter it
    //    belongs to. Decision: correlation is established at INGESTION and
    //    carried as metadata; extracted fields may VERIFY an association but
    //    never CREATE one; unassociated documents go to a human triage queue
    //    rather than being matched by guessing. Content-based matching is the
    //    option that produces confident wrong answers, and a misattributed
    //    document means the agent chases the wrong renewal -- outbound, and
    //    credibility-ending. Unattributed is a cost; misattributed is a defect.

    new cdk.CfnOutput(this, 'BdaProjectArnParamName', {
      value: this.bdaProjectArnParam.parameterName,
      description: 'SSM parameter that will hold the BDA project ARN.',
    });
  }
}
