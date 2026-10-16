/**
 * Conversation archive (docs/specs/conversation-search.md §3–§4): the per-turn
 * transcript bucket conversation search is built from, and the in-development
 * flag that gates writes to it. Pinned:
 *   - the bucket's lifecycle rule takes the RAW retention value (Memory is the
 *     one copy clamped at 365), on the `conversations/` prefix only;
 *   - unversioned, so an expiry or a delete removes the text for good;
 *   - CDK_CONVERSATION_INDEX_ENABLED is opt-in ('' and junk are off);
 *   - the runtime spends ONE env slot (the flag) and resolves the bucket from
 *     SSM; app-api gets the bucket always (deletes are not flag-gated);
 *   - the runtime can only put, under `conversations/`.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_CONVERSATION_INDEX_ENABLED';

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

function archiveBucket(template: Template): Record<string, any> {
  const buckets = Object.values(template.findResources('AWS::S3::Bucket')).filter(
    (b: any) => b.Properties?.BucketName === 'test-project-conversation-archive',
  );
  expect(buckets).toHaveLength(1);
  return buckets[0].Properties;
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

function statementsWithSid(template: Template, sid: string): any[] {
  const found: any[] = [];
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy')) as any[]) {
    for (const stmt of policy.Properties.PolicyDocument.Statement) {
      if (stmt.Sid === sid) found.push(stmt);
    }
  }
  return found;
}

describe('conversationIndex config', () => {
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
    expect(load().conversationIndex.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().conversationIndex.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().conversationIndex.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ conversationIndex: { enabled: true } }).conversationIndex.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ conversationIndex: { enabled: true } }).conversationIndex.enabled).toBe(false);
  });
});

describe('conversation archive bucket', () => {
  let template: Template;
  beforeAll(() => {
    template = synth();
  });

  it('is private, encrypted, TLS-only and unversioned', () => {
    const props = archiveBucket(template);
    expect(props.BucketEncryption.ServerSideEncryptionConfiguration[0]
      .ServerSideEncryptionByDefault.SSEAlgorithm).toBe('AES256');
    expect(props.PublicAccessBlockConfiguration).toEqual({
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    });
    expect(props.VersioningConfiguration).toBeUndefined();
  });

  it('expires conversations/ objects after the default retention', () => {
    const rules = archiveBucket(template).LifecycleConfiguration.Rules;
    const retention = rules.find((r: any) => r.Id === 'conversation-retention');
    expect(retention).toMatchObject({ Prefix: 'conversations/', ExpirationInDays: 365, Status: 'Enabled' });
  });

  it('takes the raw retention value; only Memory is clamped at 365', () => {
    const t = synth({ conversationRetentionDays: 730 });
    const rules = archiveBucket(t).LifecycleConfiguration.Rules;
    expect(rules.find((r: any) => r.Id === 'conversation-retention').ExpirationInDays).toBe(730);
    const memory = Object.values(t.findResources('AWS::BedrockAgentCore::Memory'))[0] as any;
    expect(memory.Properties.EventExpiryDuration).toBe(365);
  });

  it('publishes its name to SSM for the runtime', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/conversations/archive-bucket-name',
    });
  });
});

describe('conversation archive wiring', () => {
  it('spends one runtime env slot on the flag and none on the bucket', () => {
    const off = runtimeEnv(synth());
    expect(off.CONVERSATION_INDEX_ENABLED).toBe('false');
    expect(off).not.toHaveProperty('CONVERSATION_ARCHIVE_BUCKET_NAME');

    const on = runtimeEnv(synth({ conversationIndex: { enabled: true } }));
    expect(on.CONVERSATION_INDEX_ENABLED).toBe('true');
  });

  it('gives app-api the bucket whatever the flag says, so deletes still work', () => {
    const env = appApiEnv(synth());
    expect(env.CONVERSATION_INDEX_ENABLED).toBe('false');
    expect(env.CONVERSATION_ARCHIVE_BUCKET_NAME).toBeDefined();
  });

  it('lets the runtime put and read objects, only under conversations/, and never list or delete', () => {
    const template = synth();
    const [stmt] = statementsWithSid(template, 'ConversationArchivePut');
    expect(stmt.Action).toEqual(['s3:PutObject', 's3:GetObject']);
    expect(JSON.stringify(stmt.Resource)).toContain('/conversations/*');
  });

  it('scopes app-api list access to the conversations/ prefix', () => {
    const template = synth();
    const [list] = statementsWithSid(template, 'ConversationArchiveList');
    expect(list.Action).toBe('s3:ListBucket');
    expect(list.Condition).toEqual({ StringLike: { 's3:prefix': ['conversations/*'] } });
    const [objects] = statementsWithSid(template, 'ConversationArchiveObjects');
    expect(objects.Action).toEqual(['s3:GetObject', 's3:PutObject', 's3:DeleteObject']);
  });
});
