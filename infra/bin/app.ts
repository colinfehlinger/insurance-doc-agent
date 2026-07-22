#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { getStageConfig } from '../lib/config';
import { SharedStack } from '../lib/shared-stack';
import { StateStack } from '../lib/state-stack';
import { IngestionStack } from '../lib/ingestion-stack';
import { UnderstandingStack } from '../lib/understanding-stack';
import { AgentStack } from '../lib/agent-stack';

const app = new cdk.App();

// Stage comes from `-c stage=dev`, falling back to the default in cdk.json.
const config = getStageConfig(app.node.tryGetContext('stage'));

// Account is resolved from the caller's credentials at synth/deploy time so that
// no account id is ever committed to the repo. Region is pinned per stage.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: config.region,
};

// --- Body: the fixed, auditable pipeline ----------------------------------

const shared = new SharedStack(app, `${config.stackPrefix}-Shared`, {
  env,
  config,
  description: `Customer-managed KMS key shared by every ${config.stage} data store (${config.stackPrefix}).`,
});

const state = new StateStack(app, `${config.stackPrefix}-State`, {
  env,
  config,
  dataKey: shared.dataKey,
  description: `Matter state table: required vs received vs missing documents (${config.stackPrefix}).`,
});

const ingestion = new IngestionStack(app, `${config.stackPrefix}-Ingestion`, {
  env,
  config,
  dataKey: shared.dataKey,
  description: `Raw document landing zone for inbound email and uploads (${config.stackPrefix}).`,
});

new UnderstandingStack(app, `${config.stackPrefix}-Understanding`, {
  env,
  config,
  dataKey: shared.dataKey,
  rawBucket: ingestion.rawBucket,
  description: `STUB: Bedrock Data Automation classification and extraction (${config.stackPrefix}).`,
});

// --- Brain: the agent ------------------------------------------------------

new AgentStack(app, `${config.stackPrefix}-Agent`, {
  env,
  config,
  dataKey: shared.dataKey,
  matterTable: state.matterTable,
  description: `STUB: Bedrock AgentCore runtime that decides the next action per matter (${config.stackPrefix}).`,
});

// --- View: deliberately not instantiated yet -------------------------------
// ViewStack (React + CloudFront + API Gateway + Cognito) is defined in
// lib/view-stack.ts but stays out of the app until the owner dashboard step.

cdk.Tags.of(app).add('Project', 'insurance-doc-agent');
cdk.Tags.of(app).add('Stage', config.stage);
cdk.Tags.of(app).add('ManagedBy', 'cdk');
