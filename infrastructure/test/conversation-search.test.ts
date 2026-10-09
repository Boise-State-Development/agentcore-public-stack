/**
 * Conversation search read path (docs/specs/conversation-search.md §5). Pinned:
 *   - CDK_CONVERSATION_SEARCH_ENABLED is default-on ('' and junk are on; only
 *     'false' turns it off);
 *   - app-api gets CONVERSATION_SEARCH_ENABLED, plus the retention setting the
 *     route's read-path belt uses (§3);
 *   - the AgentCore Runtime gets neither: search is app-api only, and the
 *     Runtime's environment-variable budget is nearly spent;
 *   - app-api can already Retrieve from managed knowledge bases (the grant
 *     the search's text leg relies on), so this adds no IAM.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_CONVERSATION_SEARCH_ENABLED';

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

describe('conversationSearch config', () => {
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
    expect(load().conversationSearch.enabled).toBe(true);
  });

  it.each(['', 'true', 'yes'])('treats %j as on', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().conversationSearch.enabled).toBe(true);
  });

  it.each(['false', ' FALSE '])('turns off for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().conversationSearch.enabled).toBe(false);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ conversationSearch: { enabled: false } }).conversationSearch.enabled).toBe(false);
    process.env[FLAG_ENV] = 'true';
    expect(load({ conversationSearch: { enabled: false } }).conversationSearch.enabled).toBe(true);
  });

  it('is independent of the index flag', () => {
    process.env.CDK_CONVERSATION_INDEX_ENABLED = 'false';
    expect(load().conversationSearch.enabled).toBe(true);
  });
});

describe('conversation search wiring', () => {
  let off: Template;
  let on: Template;
  beforeAll(() => {
    off = synth({ conversationSearch: { enabled: false } });
    on = synth({ conversationSearch: { enabled: true }, conversationRetentionDays: 200 });
  });

  it('sets the flag on app-api', () => {
    expect(appApiEnv(off).CONVERSATION_SEARCH_ENABLED).toBe('false');
    expect(appApiEnv(on).CONVERSATION_SEARCH_ENABLED).toBe('true');
  });

  it('gives app-api the retention setting for the read-path belt', () => {
    expect(appApiEnv(on).CONVERSATION_RETENTION_DAYS).toBe('200');
  });

  it('spends nothing on the AgentCore Runtime', () => {
    const env = runtimeEnv(on);
    expect(env).not.toHaveProperty('CONVERSATION_SEARCH_ENABLED');
  });
});
