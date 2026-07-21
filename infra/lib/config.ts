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
}

const STAGES: Record<Stage, Omit<StageConfig, 'stackPrefix' | 'resourcePrefix' | 'ssmPrefix'>> = {
  dev: {
    stage: 'dev',
    region: 'us-east-1',
    retainData: false,
  },
  prod: {
    stage: 'prod',
    region: 'us-east-1',
    retainData: true,
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
