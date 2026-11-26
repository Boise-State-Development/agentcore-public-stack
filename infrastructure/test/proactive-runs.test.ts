/**
 * Quiet watcher flag (docs/specs/quiet-watcher.md, PR 1.1). Pinned:
 *   - CDK_PROACTIVE_RUNS_ENABLED is opt-in while in development: unset, '' and
 *     junk are off; only 'true' (any case) turns it on, and the variable beats
 *     a cdk.json context;
 *   - app-api and both scheduled-runs Lambdas get PROACTIVE_RUNS_ENABLED;
 *   - the AgentCore Runtime gets it only while on (PR 1.2), so the default
 *     spends none of the V2 environment budget;
 *   - app-api can async-invoke the scheduled-runs worker, and knows its name,
 *     only while on (answer-from-inbox PR 2.2, §3.5).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_PROACTIVE_RUNS_ENABLED';

const BASE: Record<string, unknown> = {
  projectPrefix: 'test-project',
  awsRegion: 'us-west-2',
  awsAccount: '123456789012',
  vpcCidr: '10.0.0.0/16',
  corsOrigins: 'http://localhost:4200',
  production: false,
  retainDataOnDelete: false,
  frontend: { cloudFrontPriceClass: 'PriceClass_100' },
  appApi: { cpu: 1024, memory: 2048, desiredCount: 1, maxCapacity: 2 },
  inferenceApi: {},
  fineTuning: {},
  artifacts: { retentionDays: 90, extraFrameAncestors: [] },
  mcpSandbox: { extraFrameAncestors: [] },
  ragIngestion: {
    additionalCorsOrigins: '',
    lambdaMemorySize: 10240,
    lambdaTimeout: 900,
    embeddingModel: 'amazon.titan-embed-text-v2',
    vectorDimension: 1024,
    vectorDistanceMetric: 'cosine',
  },
};

function load(ctx: Record<string, unknown> = {}): AppConfig {
  const app = new cdk.App();
  for (const [k, v] of Object.entries({ ...BASE, ...ctx })) app.node.setContext(k, v);
  return loadConfig(app);
}

function synth(enabled: boolean): Template {
  const config = createMockConfig({
    scheduledRuns: { enabled: true },
    proactiveRuns: { enabled, watchTriageEnabled: false },
  });
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function appApiEnv(template: Template): Record<string, unknown> {
  const env: Record<string, unknown> = {};
  for (const td of Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[]) {
    for (const container of td.Properties.ContainerDefinitions ?? []) {
      for (const entry of container.Environment ?? []) env[entry.Name] = entry.Value;
    }
  }
  return env;
}

function runtimeEnv(template: Template): Record<string, unknown> {
  const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
  expect(runtimes).toHaveLength(1);
  return (runtimes[0] as any).Properties.EnvironmentVariables;
}

function lambdaEnvironment(template: Template, idFragment: string): Record<string, unknown> {
  const matches = Object.entries(template.findResources('AWS::Lambda::Function'))
    .filter(([id]) => id.includes(idFragment))
    .map(([, r]) => r.Properties?.Environment?.Variables ?? {});
  expect(matches).toHaveLength(1);
  return matches[0];
}

const LAMBDAS = ['ScheduledRunsDispatcherLambda', 'ScheduledRunsWorkerLambda'];

describe('proactiveRuns config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[FLAG_ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().proactiveRuns.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().proactiveRuns.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().proactiveRuns.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ proactiveRuns: { enabled: true } }).proactiveRuns.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ proactiveRuns: { enabled: true } }).proactiveRuns.enabled).toBe(false);
  });
});

describe('proactive runs wiring', () => {
  let off: Template;
  let on: Template;
  beforeAll(() => {
    off = synth(false);
    on = synth(true);
  });

  it('sets the flag on app-api', () => {
    expect(appApiEnv(off).PROACTIVE_RUNS_ENABLED).toBe('false');
    expect(appApiEnv(on).PROACTIVE_RUNS_ENABLED).toBe('true');
  });

  it.each(LAMBDAS)('sets the flag on %s', (lambda) => {
    expect(lambdaEnvironment(off, lambda).PROACTIVE_RUNS_ENABLED).toBe('false');
    expect(lambdaEnvironment(on, lambda).PROACTIVE_RUNS_ENABLED).toBe('true');
  });

  it('writes the AgentCore Runtime copy only while on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('PROACTIVE_RUNS_ENABLED');
    expect(runtimeEnv(on).PROACTIVE_RUNS_ENABLED).toBe('true');
  });
});

/**
 * Whether app-api's task role may invoke the scheduled-runs worker, and where the statement is:
 * 'invokePolicy' for app-api's worker-invoke managed policy, 'elsewhere' for any other policy on
 * the role (its overflow policies, which CDK repacks), or false.
 */
function appApiInvokesWorker(template: Template): 'invokePolicy' | 'elsewhere' | false {
  const workerIds = Object.keys(template.findResources('AWS::Lambda::Function')).filter((id) =>
    id.includes('ScheduledRunsWorkerLambda'),
  );
  expect(workerIds).toHaveLength(1);
  const appApiRoles = Object.entries(template.findResources('AWS::ECS::TaskDefinition'))
    .filter(([id]) => id.includes('AppApi'))
    .map(([, td]) => (td as any).Properties.TaskRoleArn?.['Fn::GetAtt']?.[0])
    .filter(Boolean);
  expect(appApiRoles.length).toBeGreaterThan(0);
  let found = false as 'invokePolicy' | 'elsewhere' | false;
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const [id, policy] of Object.entries(template.findResources(type)) as [string, any][]) {
      const roles: any[] = policy.Properties.Roles ?? [];
      if (!roles.some((r) => appApiRoles.includes(r?.Ref))) continue;
      const invokes = (policy.Properties.PolicyDocument.Statement ?? []).some((st: any) => {
        const actions: string[] = [].concat(st.Action);
        return actions.includes('lambda:InvokeFunction') && JSON.stringify(st.Resource).includes(workerIds[0]);
      });
      if (invokes) found = id.includes('AppApiMemoryMaintenanceInvokePolicy') && found !== 'elsewhere' ? 'invokePolicy' : 'elsewhere';
    }
  }
  return found;
}

describe('answering from the inbox', () => {
  let off: Template;
  let on: Template;
  beforeAll(() => {
    off = synth(false);
    on = synth(true);
  });

  it('lets app-api invoke the worker only while on', () => {
    expect(appApiInvokesWorker(off)).toBe(false);
    // In app-api's worker-invoke managed policy, never the role's repacked overflow policies.
    expect(appApiInvokesWorker(on)).toBe('invokePolicy');
  });

  it("gives app-api the worker's name only while on", () => {
    expect(appApiEnv(off)).not.toHaveProperty('SCHEDULED_RUNS_WORKER_FUNCTION_NAME');
    expect(appApiEnv(on).SCHEDULED_RUNS_WORKER_FUNCTION_NAME).toBeDefined();
  });
});
