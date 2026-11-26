/**
 * Quiet watcher triage flag (docs/specs/quiet-watcher-triage.md §8, PR T.1). Pinned:
 *   - CDK_WATCH_TRIAGE_ENABLED is opt-in while in development: unset, '' and
 *     junk are off; only 'true' (any case) turns it on, and the variable beats
 *     a cdk.json context;
 *   - it is true only while the quiet watcher is (`watchTriageOn`);
 *   - the scheduled-runs worker gets WATCH_TRIAGE_ENABLED; the dispatcher and
 *     app-api don't read it and don't get it;
 *   - the AgentCore Runtime gets it only while both are on, so the default
 *     spends none of the V2 environment budget.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig, watchTriageOn } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_WATCH_TRIAGE_ENABLED';
const PARENT_ENV = 'CDK_PROACTIVE_RUNS_ENABLED';

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

function synth(enabled: boolean, watchTriageEnabled: boolean): Template {
  const config = createMockConfig({
    scheduledRuns: { enabled: true },
    proactiveRuns: { enabled, watchTriageEnabled },
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

describe('watchTriage config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[FLAG_ENV];
    delete process.env[PARENT_ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().proactiveRuns.watchTriageEnabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().proactiveRuns.watchTriageEnabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().proactiveRuns.watchTriageEnabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ proactiveRuns: { watchTriageEnabled: true } }).proactiveRuns.watchTriageEnabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ proactiveRuns: { watchTriageEnabled: true } }).proactiveRuns.watchTriageEnabled).toBe(false);
  });

  it('is on only while the quiet watcher is', () => {
    process.env[FLAG_ENV] = 'true';
    expect(watchTriageOn(load())).toBe(false);
    process.env[PARENT_ENV] = 'true';
    expect(watchTriageOn(load())).toBe(true);
  });
});

describe('watch triage wiring', () => {
  let off: Template;
  let parentOnly: Template;
  let onWithoutParent: Template;
  let on: Template;
  beforeAll(() => {
    off = synth(false, false);
    parentOnly = synth(true, false);
    onWithoutParent = synth(false, true);
    on = synth(true, true);
  });

  it('sets the flag on the worker, true only while both are on', () => {
    expect(lambdaEnvironment(off, 'ScheduledRunsWorkerLambda').WATCH_TRIAGE_ENABLED).toBe('false');
    expect(lambdaEnvironment(parentOnly, 'ScheduledRunsWorkerLambda').WATCH_TRIAGE_ENABLED).toBe('false');
    expect(lambdaEnvironment(onWithoutParent, 'ScheduledRunsWorkerLambda').WATCH_TRIAGE_ENABLED).toBe('false');
    expect(lambdaEnvironment(on, 'ScheduledRunsWorkerLambda').WATCH_TRIAGE_ENABLED).toBe('true');
  });

  it('leaves the dispatcher and app-api without it', () => {
    expect(lambdaEnvironment(on, 'ScheduledRunsDispatcherLambda')).not.toHaveProperty('WATCH_TRIAGE_ENABLED');
    expect(appApiEnv(on)).not.toHaveProperty('WATCH_TRIAGE_ENABLED');
  });

  it('writes the AgentCore Runtime copy only while both are on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('WATCH_TRIAGE_ENABLED');
    expect(runtimeEnv(parentOnly)).not.toHaveProperty('WATCH_TRIAGE_ENABLED');
    expect(runtimeEnv(onWithoutParent)).not.toHaveProperty('WATCH_TRIAGE_ENABLED');
    expect(runtimeEnv(on).WATCH_TRIAGE_ENABLED).toBe('true');
  });
});
