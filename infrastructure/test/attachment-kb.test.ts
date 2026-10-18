/**
 * The shared `attachments` knowledge base, PR-3 (docs/specs/large-document-attachments.md
 * §4E), folded into the large-document pipeline because PlatformStack sits at
 * 492/500 resources on dev. Pinned:
 *   - CDK_ATTACHMENT_KB_ENABLED is opt-in, and refused without large-document attachments;
 *   - on, it costs exactly 2 resources: an S3 Object Deleted rule and a daily
 *     schedule, both targeting the existing prepare queue; the user-files
 *     bucket's EventBridge delivery is its own property (no notification custom
 *     resource, no handler policy); no new role, policy or managed policy;
 *   - the prepare Lambda gets the flag, the KB settings, the byte-accounting and
 *     session-activity grants scoped to their keys, and the KB grants; its event
 *     source drops to two in flight;
 *   - the KB service role may read originals under `user-files/` only;
 *   - app-api gets the flag; the Runtime gets it only while on (PR-4: it adds
 *     document_read's `query` argument), and nothing else changes there.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import {
  LARGE_DOCUMENT_PREPARE_ESM,
  LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES,
  LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES_WITH_KB,
} from '../lib/constructs/large-document/large-document-prepare-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

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

interface PolicyStatement {
  Sid?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, unknown>;
}

const actionsOf = (s: PolicyStatement): string[] => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']);

function statements(template: Template, idPattern: RegExp): PolicyStatement[] {
  return Object.entries(template.findResources('AWS::IAM::Policy'))
    .filter(([id]) => idPattern.test(id))
    .flatMap(([, r]) => (r as any).Properties.PolicyDocument.Statement as PolicyStatement[]);
}

function prepareFunction(template: Template): Record<string, any> {
  const matches = Object.entries(template.findResources('AWS::Lambda::Function'))
    .filter(([id]) => id.includes('LargeDocumentPrepareLambda'))
    .map(([, r]) => (r as any).Properties);
  expect(matches).toHaveLength(1);
  return matches[0];
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

describe('attachmentKb config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env.CDK_ATTACHMENT_KB_ENABLED;
    delete process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED;
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().attachmentKb.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes'])('treats %j as off', (value) => {
    process.env.CDK_ATTACHMENT_KB_ENABLED = value;
    expect(load().attachmentKb.enabled).toBe(false);
  });

  it('turns on with large-document attachments', () => {
    process.env.CDK_ATTACHMENT_KB_ENABLED = 'true';
    process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED = 'true';
    expect(load().attachmentKb.enabled).toBe(true);
  });

  it('turns on with large-document attachments left at their default', () => {
    process.env.CDK_ATTACHMENT_KB_ENABLED = 'true';
    expect(load().attachmentKb.enabled).toBe(true);
  });

  it('is refused with large-document attachments turned off', () => {
    process.env.CDK_ATTACHMENT_KB_ENABLED = 'true';
    process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED = 'false';
    expect(() => load()).toThrow(/requires CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED/);
  });
});

describe('the attachments KB in PlatformStack', () => {
  const docsOnly = synth({ largeDocumentAttachments: { enabled: true } });
  const withKb = synth({ largeDocumentAttachments: { enabled: true }, attachmentKb: { enabled: true } });

  it('costs exactly 2 resources, none of them IAM', () => {
    const before = new Set(Object.keys(docsOnly.toJSON().Resources));
    const added = Object.entries(withKb.toJSON().Resources)
      .filter(([id]) => !before.has(id))
      .map(([, r]) => (r as any).Type)
      .sort();
    expect(added).toEqual(['AWS::Events::Rule', 'AWS::Events::Rule']);
  });

  it('turns on the user-files bucket\'s EventBridge delivery through the bucket itself', () => {
    const userFiles = (t: Template) =>
      Object.entries(t.findResources('AWS::S3::Bucket')).find(([id]) => id.startsWith('FileUploadUserFilesBucket'))![1] as any;
    expect(userFiles(withKb).Properties.NotificationConfiguration).toEqual({
      EventBridgeConfiguration: { EventBridgeEnabled: true },
    });
    expect(userFiles(docsOnly).Properties.NotificationConfiguration).toBeUndefined();
  });

  it('routes deleted originals and the daily reconcile to the prepare queue', () => {
    const rules = Object.values(withKb.findResources('AWS::Events::Rule')).map((r: any) => r.Properties);
    const deleted = rules.find((r) => String(r.Name ?? '').includes('attachment-kb-object-deleted'));
    const reconcile = rules.find((r) => String(r.Name ?? '').includes('attachment-kb-reconcile'));
    expect(deleted.EventPattern).toMatchObject({
      source: ['aws.s3'],
      'detail-type': ['Object Deleted'],
      detail: { object: { key: [{ 'anything-but': { wildcard: ['*/_derived/*', '*/_thumb.png'] } }] } },
    });
    expect(reconcile.ScheduleExpression).toBe('cron(40 9 * * ? *)');
    expect(JSON.parse(reconcile.Targets[0].Input)).toEqual({ action: 'reconcile' });
    for (const rule of [deleted, reconcile]) {
      expect(JSON.stringify(rule.Targets[0].Arn)).toContain('LargeDocumentPrepareQueue');
    }
  });

  it('gives the prepare Lambda the flag and the KB settings', () => {
    const env = prepareFunction(withKb).Environment.Variables;
    expect(env.ATTACHMENT_KB_ENABLED).toBe('true');
    for (const name of [
      'LARGE_DOCUMENT_PREPARE_QUEUE_URL',
      'DYNAMODB_ASSISTANTS_TABLE_NAME',
      'DYNAMODB_SESSIONS_METADATA_TABLE_NAME',
      'MANAGED_KB_SERVICE_ROLE_ARN',
      'MANAGED_KB_METRIC_NAMESPACE',
      'MANAGED_KB_TAG_VALUE_PREFIX',
      'MANAGED_KB_TAG_VALUE_ENVIRONMENT',
    ]) {
      expect(env).toHaveProperty(name);
    }
    expect(prepareFunction(docsOnly).Environment.Variables).not.toHaveProperty('ATTACHMENT_KB_ENABLED');
  });

  it('holds the byte accounting to its own partition and the session read to one index', () => {
    const s = statements(withKb, /LargeDocumentPrepare/);
    const bytes = s.find((x) => x.Sid === 'AttachmentKbRecordAndBytes')!;
    expect(bytes.Condition).toEqual({
      'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['AST#attachments'] },
    });
    const sessions = s.find((x) => x.Sid === 'AttachmentKbSessionActivity')!;
    expect(actionsOf(sessions)).toEqual(['dynamodb:Query']);
    expect(JSON.stringify(sessions.Resource)).toContain('/index/SessionLookupIndex');
    const all = s.flatMap(actionsOf);
    for (const action of [
      'sqs:SendMessage',
      'bedrock:IngestKnowledgeBaseDocuments',
      'bedrock:DeleteKnowledgeBaseDocuments',
      'bedrock:ListKnowledgeBaseDocuments',
      'bedrock:CreateKnowledgeBase',
      'iam:PassRole',
    ]) {
      expect(all).toContain(action);
    }
    expect(all).not.toContain('bedrock:DeleteKnowledgeBase');
  });

  it('lets the KB service role read originals under user-files/ only', () => {
    const s = statements(withKb, /ManagedKbServiceRole/).find((x) => x.Sid === 'AttachmentKbUserFilesRead')!;
    expect(actionsOf(s)).toEqual(['s3:GetObject']);
    expect(JSON.stringify(s.Resource)).toContain('/user-files/*');
    expect(statements(docsOnly, /ManagedKbServiceRole/).some((x) => x.Sid === 'AttachmentKbUserFilesRead')).toBe(false);
  });

  it('keeps the prepare role\'s inline policy inside IAM\'s 10,240-character limit', () => {
    // CDK does not check this; IAM refuses the deploy. Every intrinsic (Ref,
    // GetAtt, Join, Sub) is counted as a 120-character ARN, longer than any it
    // resolves to here, so the estimate errs high.
    const estimate = (value: unknown): unknown => {
      if (Array.isArray(value)) return value.map(estimate);
      if (value && typeof value === 'object') {
        const keys = Object.keys(value as object);
        if (keys.length === 1 && (keys[0] === 'Ref' || keys[0].startsWith('Fn::'))) return 'x'.repeat(120);
        return Object.fromEntries(Object.entries(value as object).map(([k, v]) => [k, estimate(v)]));
      }
      return value;
    };
    const [policy] = Object.entries(withKb.findResources('AWS::IAM::Policy'))
      .filter(([id]) => /LargeDocumentPrepareLambdaServiceRoleDefaultPolicy/.test(id))
      .map(([, r]) => (r as any).Properties.PolicyDocument);
    const size = JSON.stringify(estimate(policy)).length;
    expect(size).toBeLessThan(10_240);
  });

  it('gives the function time to provision the knowledge base on its first ingest', () => {
    expect(prepareFunction(withKb).Timeout).toBe(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES_WITH_KB * 60);
    expect(prepareFunction(docsOnly).Timeout).toBe(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES * 60);
    const visibility = (t: Template) =>
      (Object.values(t.findResources('AWS::SQS::Queue')) as any[])
        .map((q) => q.Properties)
        .find((q) => q.QueueName === 'test-project-large-document-prepare' || String(q.QueueName).endsWith('large-document-prepare'))
        .VisibilityTimeout;
    expect(visibility(withKb)).toBe(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES_WITH_KB * 60 * 6);
  });

  it('drops the event source to two in flight', () => {
    const maxConcurrency = (t: Template) =>
      (Object.values(t.findResources('AWS::Lambda::EventSourceMapping')) as any[])
        .filter((m) => JSON.stringify(m.Properties.EventSourceArn).includes('LargeDocumentPrepareQueue'))
        .map((m) => m.Properties.ScalingConfig.MaximumConcurrency);
    expect(maxConcurrency(withKb)).toEqual([LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY_WITH_KB]);
    expect(maxConcurrency(docsOnly)).toEqual([LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY]);
  });

  it('gives app-api the flag, and the Runtime the flag only while on', () => {
    expect(appApiEnv(withKb).ATTACHMENT_KB_ENABLED).toBe('true');
    expect(appApiEnv(docsOnly).ATTACHMENT_KB_ENABLED).toBe('false');
    const runtimeEnv = (t: Template) =>
      (Object.values(t.findResources('AWS::BedrockAgentCore::Runtime'))[0] as any).Properties.EnvironmentVariables;
    // Off spends no bytes of the V2 environment budget.
    expect(runtimeEnv(docsOnly)).not.toHaveProperty('ATTACHMENT_KB_ENABLED');
    const { ATTACHMENT_KB_ENABLED, ...rest } = runtimeEnv(withKb);
    expect(ATTACHMENT_KB_ENABLED).toBe('true');
    expect(rest).toEqual(runtimeEnv(docsOnly));
  });
});
