/**
 * Conversation retention: one setting, CDK_CONVERSATION_RETENTION_DAYS, for
 * every copy of a conversation's content (docs/specs/conversation-search.md §3).
 *
 * Today it drives one copy, AgentCore Memory's `eventExpiryDuration`. Memory
 * held 90 days before this setting existed, and 11% of prod's active sessions
 * had outlived their events, listed in the sidebar but opening empty. Pinned:
 *   - unset and '' (an unset GitHub variable) both mean 365;
 *   - below Memory's minimum of 3, or anything that is not a whole number,
 *     fails synth instead of quietly applying some other number;
 *   - above 365 is accepted in config (the archive in PR-2 uses the raw
 *     value) and clamped only where Memory reads it.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, CONVERSATION_RETENTION_DAYS_DEFAULT, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const ENV = 'CDK_CONVERSATION_RETENTION_DAYS';

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

function memoryExpiry(conversationRetentionDays: number): unknown {
  const config = createMockConfig({ conversationRetentionDays });
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  const memories = Object.values(
    Template.fromStack(stack).findResources('AWS::BedrockAgentCore::Memory'),
  );
  expect(memories).toHaveLength(1);
  return (memories[0].Properties as Record<string, unknown>).EventExpiryDuration;
}

describe('conversationRetentionDays config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('defaults to 365 when the variable is unset', () => {
    expect(CONVERSATION_RETENTION_DAYS_DEFAULT).toBe(365);
    expect(load().conversationRetentionDays).toBe(365);
  });

  it('treats "" (an unset GitHub variable) as unset, not 0', () => {
    process.env[ENV] = '';
    expect(load().conversationRetentionDays).toBe(365);
  });

  it('reads the variable', () => {
    process.env[ENV] = '180';
    expect(load().conversationRetentionDays).toBe(180);
  });

  it('falls back to context, and the variable beats context', () => {
    expect(load({ conversationRetentionDays: 120 }).conversationRetentionDays).toBe(120);
    expect(load({ conversationRetentionDays: '120' }).conversationRetentionDays).toBe(120);
    process.env[ENV] = '200';
    expect(load({ conversationRetentionDays: 120 }).conversationRetentionDays).toBe(200);
  });

  it('accepts Memory\'s minimum of 3', () => {
    process.env[ENV] = '3';
    expect(load().conversationRetentionDays).toBe(3);
  });

  it('keeps a value above 365 unclamped in config, for the archive', () => {
    process.env[ENV] = '730';
    expect(load().conversationRetentionDays).toBe(730);
  });

  it.each(['2', '0'])('rejects %s, below Memory\'s minimum, at synth', (value) => {
    process.env[ENV] = value;
    expect(() => load()).toThrow(/conversationRetentionDays: \d+.*at least 3/);
  });

  // parseIntEnv alone would turn these into 30, undefined (→ 365) and -5.
  it.each(['30.5', 'abc', '-5', '90d'])('rejects %s, which is not a whole number', (value) => {
    process.env[ENV] = value;
    expect(() => load()).toThrow(/CDK_CONVERSATION_RETENTION_DAYS.*whole number/);
  });

  it('rejects a non-integer from context too', () => {
    expect(() => load({ conversationRetentionDays: 30.5 })).toThrow(/whole number/);
  });
});

describe('AgentCore Memory eventExpiryDuration', () => {
  it('is 365 by default', () => {
    expect(memoryExpiry(CONVERSATION_RETENTION_DAYS_DEFAULT)).toBe(365);
  });

  it('follows the configured retention', () => {
    expect(memoryExpiry(30)).toBe(30);
  });

  it('clamps to Memory\'s 365-day maximum', () => {
    expect(memoryExpiry(730)).toBe(365);
  });
});

// Session pruning (PR-2c): a feature switch that defaults on, and an arming
// flag that defaults off, so an upgraded deployment reports and deletes
// nothing until it arms.
describe('conversation retention pruning config', () => {
  const saved = { ...process.env };
  const PRUNES = 'CDK_CONVERSATION_RETENTION_PRUNES_SESSIONS';
  const ARMED = 'CDK_CONVERSATION_RETENTION_PRUNE_ARMED';

  beforeEach(() => {
    delete process.env[PRUNES];
    delete process.env[ARMED];
    jest.spyOn(console, 'log').mockImplementation(() => undefined);
    jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    jest.restoreAllMocks();
  });

  it.each([undefined, ''])('prunes sessions by default (%p) and is disarmed by default', (value) => {
    if (value !== undefined) {
      process.env[PRUNES] = value;
      process.env[ARMED] = value;
    }
    const config = load();
    expect(config.conversationRetentionPrunesSessions).toBe(true);
    expect(config.conversationRetentionPruneArmed).toBe(false);
  });

  it('opts out with "false" and arms with "true"', () => {
    process.env[PRUNES] = 'false';
    process.env[ARMED] = 'true';
    const config = load();
    expect(config.conversationRetentionPrunesSessions).toBe(false);
    expect(config.conversationRetentionPruneArmed).toBe(true);
  });

  it('reads boolean cdk.json context', () => {
    const config = load({ conversationRetentionPrunesSessions: false, conversationRetentionPruneArmed: true });
    expect(config.conversationRetentionPrunesSessions).toBe(false);
    expect(config.conversationRetentionPruneArmed).toBe(true);
  });

  it('fails synth on a value it cannot read rather than guessing', () => {
    process.env[ARMED] = 'yes please';
    expect(() => load()).toThrow(/Invalid boolean/);
  });
});
