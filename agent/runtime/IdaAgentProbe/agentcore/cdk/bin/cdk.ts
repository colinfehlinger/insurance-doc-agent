#!/usr/bin/env node
import { AgentCoreStack, type HarnessConfig } from '../lib/cdk-stack';
import { ConfigIO, HarnessSpecSchema, type AwsDeploymentTarget } from '@aws/agentcore-cdk';
import { App, Tags, type Environment } from 'aws-cdk-lib';
import * as path from 'path';
import * as fs from 'fs';

function toEnvironment(target: AwsDeploymentTarget): Environment {
  return {
    account: target.account,
    region: target.region,
  };
}

function sanitize(name: string): string {
  return name.replace(/_/g, '-');
}

// DO NOT CHANGE THIS FORMAT. The stack name is NOT a controllable knob: the
// agentcore CLI recomputes `AgentCore-<project>-<target>` itself and asserts the
// synthesized stack matches. Renaming to the project's `Ida-Dev-AgentProbe`
// convention was tried and fails at the Synthesize step with:
//
//   Synthesized stacks [Ida-Dev-AgentProbe] do not include the stack for
//   target "dev" (expected "AgentCore-IdaAgentProbe-dev").
//
// The closest available knob is the PROJECT NAME (agentcore.json -> name),
// which is why it is `IdaAgentProbe` -- that carries the `Ida` prefix into the
// stack name and into every CDK-auto-named child resource. Recognizability is
// achieved via the project name and the Tags.of(app) block below, not here.
function toStackName(projectName: string, targetName: string): string {
  return `AgentCore-${sanitize(projectName)}-${sanitize(targetName)}`;
}

async function main() {
  // Config root is parent of cdk/ directory. The CLI sets process.cwd() to agentcore/cdk/.
  const configRoot = path.resolve(process.cwd(), '..');
  const configIO = new ConfigIO({ baseDir: configRoot });

  const spec = await configIO.readProjectSpec();
  const targets = await configIO.readAWSDeploymentTargets();

  // The vended CDK project compiles against the published @aws/agentcore-cdk
  // schema type, which may lag the CLI's own AgentCoreProjectSpec (e.g. payments,
  // harnesses, gateway fields). Cast once so those fields are reachable.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const specAny = spec as any;

  // Extract MCP configuration from project spec.
  // Gateway fields are stored in agentcore.json but may not yet be on the
  const mcpSpec = specAny.agentCoreGateways?.length
    ? {
        agentCoreGateways: specAny.agentCoreGateways,
        mcpRuntimeTools: specAny.mcpRuntimeTools,
        unassignedTargets: specAny.unassignedTargets,
      }
    : undefined;

  // Read deployed state for credential ARNs (populated by pre-deploy identity setup)
  let deployedState: Record<string, unknown> | undefined;
  try {
    deployedState = JSON.parse(fs.readFileSync(path.join(configRoot, '.cli', 'deployed-state.json'), 'utf8'));
  } catch {
    // Deployed state may not exist on first deploy
  }

  if (targets.length === 0) {
    throw new Error('No deployment targets configured. Please define targets in agentcore/aws-targets.json');
  }

  // Read harness configs: the full validated spec drives the CFN resource; the
  // role-scoped fields drive the IAM role + container build.
  const projectRoot = path.resolve(configRoot, '..');

  // Read non-S3 KB connector-config files and pass their parsed contents to the
  // L3 verbatim. The L3 does not read files; it expects the parsed
  // connectorParameters keyed by the data source's connectorConfigFile path.
  const connectorParametersByFile: Record<string, Record<string, unknown>> = {};
  for (const kb of specAny.knowledgeBases ?? []) {
    for (const ds of kb.dataSources ?? []) {
      if (ds.type !== 'S3' && ds.connectorConfigFile) {
        const abs = path.resolve(projectRoot, ds.connectorConfigFile);
        try {
          connectorParametersByFile[ds.connectorConfigFile] = JSON.parse(fs.readFileSync(abs, 'utf-8'));
        } catch (err) {
          throw new Error(
            `Could not read connector config '${ds.connectorConfigFile}' for knowledge base '${kb.name}' at ${abs}: ${err instanceof Error ? err.message : err}`
          );
        }
      }
    }
  }

  // Synthesize an AWS::BedrockAgentCore::Harness resource for each harness entry in the spec.
  const harnessConfigs: HarnessConfig[] = [];
  for (const entry of specAny.harnesses ?? []) {
    const harnessDir = path.resolve(projectRoot, entry.path);
    const harnessPath = path.resolve(harnessDir, 'harness.json');
    try {
      const harnessSpec = HarnessSpecSchema.parse(JSON.parse(fs.readFileSync(harnessPath, 'utf-8')));
      harnessConfigs.push({
        name: entry.name,
        executionRoleArn: harnessSpec.executionRoleArn,
        // Only an `existing` memory ref carries a name to wire IAM against; managed memory is
        // owned by the harness (no sibling) and disabled has none — both resolve to undefined.
        memoryName: harnessSpec.memory?.mode === 'existing' ? harnessSpec.memory.name : undefined,
        containerUri: harnessSpec.containerUri,
        hasDockerfile: !!harnessSpec.dockerfile,
        dockerfile: harnessSpec.dockerfile,
        codeLocation: harnessSpec.dockerfile ? harnessDir : undefined,
        tools: harnessSpec.tools,
        skills: harnessSpec.skills,
        apiKeyArn: harnessSpec.model?.apiKeyArn,
        efsAccessPoints: harnessSpec.efsAccessPoints,
        s3AccessPoints: harnessSpec.s3AccessPoints,
        apiFormat: harnessSpec.model?.apiFormat,
        // Full spec + dir drive the AWS::BedrockAgentCore::Harness CFN resource.
        spec: harnessSpec,
        harnessDir,
      });
    } catch (err) {
      throw new Error(
        `Could not read harness.json for "${entry.name}" at ${harnessPath}: ${err instanceof Error ? err.message : err}`
      );
    }
  }

  const app = new App();

  for (const target of targets) {
    const env = toEnvironment(target);
    const stackName = toStackName(spec.name, target.name);

    // Extract credentials from deployed state for this target
    const targetState = (deployedState as Record<string, unknown>)?.targets as
      Record<string, Record<string, unknown>> | undefined;
    const targetResources = targetState?.[target.name]?.resources as Record<string, unknown> | undefined;
    const credentials = targetResources?.credentials as
      Record<string, { credentialProviderArn: string; clientSecretArn?: string }> | undefined;

    // Payment credential provider ARNs live in the same credentials map as identity credentials
    const paymentCredentials = credentials;

    const paymentSpec = specAny.payments?.length
      ? specAny.payments.map(
          (p: {
            name: string;
            description?: string;
            authorizerType: 'AWS_IAM' | 'CUSTOM_JWT';
            authorizerConfiguration?: unknown;
            autoPayment?: boolean;
            paymentToolAllowlist?: string[];
            networkPreferences?: string[];
            connectors: { name: string; provider?: string; credentialName: string }[];
          }) => ({
            name: p.name,
            description: p.description,
            authorizerType: p.authorizerType,
            authorizerConfiguration: p.authorizerConfiguration,
            autoPayment: p.autoPayment,
            paymentToolAllowlist: p.paymentToolAllowlist,
            networkPreferences: p.networkPreferences,
            connectors: p.connectors.map(c => {
              const credentialProviderArn = paymentCredentials?.[c.credentialName]?.credentialProviderArn;
              if (!credentialProviderArn) {
                // Fail fast with an actionable message rather than passing an empty
                // ARN that fails opaquely server-side at CreatePaymentConnector.
                throw new Error(
                  `Payment connector "${c.name}" on manager "${p.name}" references credential ` +
                    `"${c.credentialName}", but no deployed credential provider was found for it. ` +
                    `Run \`agentcore deploy\` so the credential provider is created first.`
                );
              }
              return { name: c.name, provider: c.provider, credentialProviderArn };
            }),
          })
        )
      : undefined;

    new AgentCoreStack(app, stackName, {
      spec,
      mcpSpec,
      credentials,
      connectorParametersByFile,
      harnesses: harnessConfigs.length > 0 ? harnessConfigs : undefined,
      paymentSpec,
      env,
      description: `AgentCore stack for ${spec.name} deployed to ${target.name} (${target.region})`,
      tags: {
        'agentcore:project-name': spec.name,
        'agentcore:target-name': target.name,
      },
    });
  }

  // PROJECT CONVENTION (insurance-doc-agent) -- mirrors Tags.of(app) in
  // infra/bin/app.ts so the whole probe stack inherits them. These matter:
  // an untagged resource is invisible to resourcegroupstaggingapi, which is
  // exactly how a legacy table survived the account decommission unnoticed
  // (see docs/repo-hygiene-lessons.md).
  Tags.of(app).add('project', 'insurance-doc-agent');
  Tags.of(app).add('stage', targets[0]!.name);
  Tags.of(app).add('component', 'agent-probe');

  app.synth();
}

main().catch((error: unknown) => {
  console.error('AgentCore CDK synthesis failed:', error instanceof Error ? error.message : error);
  process.exit(1);
});
