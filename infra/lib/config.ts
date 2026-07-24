/**
 * Stage configuration.
 *
 * Only `dev` is deployed today. `prod` exists so that adding it later is a
 * one-line change in bin/app.ts rather than a refactor. The only behavioural
 * difference that matters right now is `retainData`: prod keeps stateful
 * resources when a stack is deleted, dev throws them away.
 */

/** Namespace for every stack id and physical resource name in this project. */
export const PROJECT_PREFIX = 'Ida';

/** Lowercase form used for physical resource names (S3, DynamoDB, KMS alias, SSM). */
export const RESOURCE_PREFIX = 'ida';

export type Stage = 'dev' | 'prod';

export interface StageConfig {
  /** The stage name, e.g. 'dev'. */
  readonly stage: Stage;
  /** Region for every stack in this stage. Fixed to us-east-1 for now. */
  readonly region: string;
  /**
   * When true, stateful resources (KMS key, DynamoDB table, S3 bucket) survive
   * a `cdk destroy`. When false they are deleted along with their contents.
   */
  readonly retainData: boolean;
  /** Stack id prefix, e.g. 'Ida-Dev' -> stacks are 'Ida-Dev-Shared', ... */
  readonly stackPrefix: string;
  /** Physical resource name prefix, e.g. 'ida-dev' -> 'ida-dev-matters'. */
  readonly resourcePrefix: string;
  /** SSM parameter namespace, e.g. '/ida/dev'. */
  readonly ssmPrefix: string;
  /**
   * Per-field extraction confidence below which a document routes to human
   * review instead of being accepted straight into matter state. See ADR-002 --
   * this starts conservative and tightens only once calibration data exists.
   */
  readonly extractionConfidenceThreshold: number;
  /** Outbound reminder settings for the agent's send_reminder tool. */
  readonly messaging: MessagingConfig;
}

/**
 * Configuration for the agent's send_reminder tool. Kept in config (surfaced as
 * SSM parameters), never hard-coded in Lambda source, so the recipient and
 * sender can change per stage without a code change.
 */
export interface MessagingConfig {
  /**
   * Where reminder emails are sent in this stage. SES production access is
   * enabled on this account, so no per-recipient verification is required -- but
   * see senderAddress: the recipient still has to be an address that can
   * actually receive, which the gmail can and owner@example.com cannot yet.
   */
  readonly testRecipient: string;
  /**
   * The From address. SES requires the sender identity (address or its domain)
   * to be verified before it can send at all. As of 2026-07-22 the ONLY verified
   * identity in this account is test-recipient@example.com, and fehlingerops.com
   * is not yet registered (ADR-005 domain track).
   *
   * send_reminder is the final beat of the Step 5 slice, so both sender and
   * recipient are pinned to the verified gmail for this run -- otherwise the
   * milestone would fail at the last step on an unverified identity. Swap both
   * to the real domain (sender no-reply@..., recipient/aliases docs+{token}@...)
   * once that domain is registered and SES-verified per the ADR-005 track.
   */
  readonly senderAddress: string;
}

// Pinned for this run: the only verified SES identity. See MessagingConfig.
const VERIFIED_SENDER_IDENTITY = 'test-recipient@example.com';

const STAGES: Record<Stage, Omit<StageConfig, 'stackPrefix' | 'resourcePrefix' | 'ssmPrefix'>> = {
  dev: {
    stage: 'dev',
    region: 'us-east-1',
    retainData: false,
    extractionConfidenceThreshold: 0.8,
    messaging: {
      testRecipient: VERIFIED_SENDER_IDENTITY,
      senderAddress: VERIFIED_SENDER_IDENTITY,
    },
  },
  prod: {
    stage: 'prod',
    region: 'us-east-1',
    retainData: true,
    extractionConfidenceThreshold: 0.9,
    messaging: {
      testRecipient: VERIFIED_SENDER_IDENTITY,
      senderAddress: VERIFIED_SENDER_IDENTITY,
    },
  },
};

function isStage(value: string): value is Stage {
  return value === 'dev' || value === 'prod';
}

/**
 * Resolve a stage name (from `-c stage=...` or cdk.json context) into a full
 * config. Throws on an unknown stage so a typo fails at synth, not at deploy.
 */
export function getStageConfig(stage: string | undefined): StageConfig {
  if (!stage) {
    throw new Error(
      "No stage supplied. Pass '-c stage=dev' or set a default in cdk.json context.",
    );
  }
  if (!isStage(stage)) {
    throw new Error(`Unknown stage '${stage}'. Valid stages: ${Object.keys(STAGES).join(', ')}.`);
  }

  const base = STAGES[stage];
  const titleCased = stage.charAt(0).toUpperCase() + stage.slice(1);

  return {
    ...base,
    stackPrefix: `${PROJECT_PREFIX}-${titleCased}`,
    resourcePrefix: `${RESOURCE_PREFIX}-${stage}`,
    ssmPrefix: `/${RESOURCE_PREFIX}/${stage}`,
  };
}

/** Props every stack in this project receives. */
export interface IdaStackPropsBase {
  readonly config: StageConfig;
}
