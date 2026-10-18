/**
 * Word tracked changes (docs/specs/word-tracked-changes.md). Pinned:
 *   - CDK_WORD_TRACKED_CHANGES_ENABLED is opt-in ('' and junk are off);
 *   - the Runtime gets WORD_TRACKED_CHANGES_ENABLED only while on, so an
 *     environment that leaves it off spends none of the V2 environment budget;
 *   - app-api always carries it, matching the Runtime, because the upload digest
 *     it builds numbers paragraphs the way the Runtime's document_read does.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_WORD_TRACKED_CHANGES_ENABLED';

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

function synth(overrides: Partial<AppConfig> = {}): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function runtimeEnv(template: Template): Record<string, unknown> {
  const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
  expect(runtimes).toHaveLength(1);
  return (runtimes[0] as any).Properties.EnvironmentVariables;
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

describe('wordTrackedChanges config', () => {
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

  it('is on by default', () => {
    expect(load().wordTrackedChanges.enabled).toBe(true);
  });

  it.each(['', 'true', 'yes', '1'])('treats %j as on', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().wordTrackedChanges.enabled).toBe(true);
  });

  it.each(['false', ' FALSE '])('turns off for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().wordTrackedChanges.enabled).toBe(false);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ wordTrackedChanges: { enabled: false } }).wordTrackedChanges.enabled).toBe(false);
    process.env[FLAG_ENV] = 'true';
    expect(load({ wordTrackedChanges: { enabled: false } }).wordTrackedChanges.enabled).toBe(true);
  });
});

describe('word tracked changes wiring', () => {
  it('spends no Runtime env bytes while on (the default), and sets the flag only while off', () => {
    expect(runtimeEnv(synth({ wordTrackedChanges: { enabled: true } }))).not.toHaveProperty('WORD_TRACKED_CHANGES_ENABLED');
    expect(runtimeEnv(synth({ wordTrackedChanges: { enabled: false } })).WORD_TRACKED_CHANGES_ENABLED).toBe('false');
  });

  it('sets app-api to match the Runtime, on and off', () => {
    expect(appApiEnv(synth()).WORD_TRACKED_CHANGES_ENABLED).toBe('false');
    expect(appApiEnv(synth({ wordTrackedChanges: { enabled: true } })).WORD_TRACKED_CHANGES_ENABLED).toBe('true');
  });
});
