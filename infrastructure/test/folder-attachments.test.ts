/**
 * Folder attachments, PR-A (docs/specs/drive-folder-attachments.md §4.8). Pinned:
 *   - CDK_FOLDER_ATTACHMENTS_ENABLED is opt-in, and refused without large-document
 *     attachments (a folder's files are read through the prepare Lambda's sidecars);
 *   - on, it sets FOLDER_ATTACHMENTS_ENABLED on app-api and the Runtime and adds no
 *     resource; off, the Runtime spends no environment bytes on it (PR-B).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
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

function appApiEnv(template: Template): Record<string, unknown> {
  const env: Record<string, unknown> = {};
  for (const td of Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[]) {
    for (const container of td.Properties.ContainerDefinitions ?? []) {
      for (const entry of container.Environment ?? []) env[entry.Name] = entry.Value;
    }
  }
  return env;
}

describe('folderAttachments config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env.CDK_FOLDER_ATTACHMENTS_ENABLED;
    delete process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED;
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is on by default', () => {
    expect(load().folderAttachments.enabled).toBe(true);
  });

  it.each(['', 'true', 'yes'])('treats %j as on', (value) => {
    process.env.CDK_FOLDER_ATTACHMENTS_ENABLED = value;
    expect(load().folderAttachments.enabled).toBe(true);
  });

  it('treats "false" as off', () => {
    process.env.CDK_FOLDER_ATTACHMENTS_ENABLED = ' False ';
    expect(load().folderAttachments.enabled).toBe(false);
  });

  it('follows large-document attachments off when unset, instead of failing synth', () => {
    process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED = 'false';
    expect(load().folderAttachments.enabled).toBe(false);
    process.env.CDK_FOLDER_ATTACHMENTS_ENABLED = '';
    expect(load().folderAttachments.enabled).toBe(false);
  });

  it('is refused when set on with large-document attachments off', () => {
    process.env.CDK_FOLDER_ATTACHMENTS_ENABLED = 'true';
    process.env.CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED = 'false';
    expect(() => load()).toThrow(/CDK_FOLDER_ATTACHMENTS_ENABLED=true requires CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED/);
  });

  it('reads the cdk.json context when the variable is unset', () => {
    expect(load({ folderAttachments: { enabled: false } }).folderAttachments.enabled).toBe(false);
    expect(load({ largeDocumentAttachments: { enabled: false } }).folderAttachments.enabled).toBe(false);
  });
});

describe('folder attachments in PlatformStack', () => {
  const docsOnly = synth({ largeDocumentAttachments: { enabled: true } });
  const withFolders = synth({ largeDocumentAttachments: { enabled: true }, folderAttachments: { enabled: true } });

  it('gives app-api the flag, and the Runtime the flag only while off', () => {
    expect(appApiEnv(withFolders).FOLDER_ATTACHMENTS_ENABLED).toBe('true');
    expect(appApiEnv(docsOnly).FOLDER_ATTACHMENTS_ENABLED).toBe('false');
    const runtimeEnv = (t: Template) =>
      (Object.values(t.findResources('AWS::BedrockAgentCore::Runtime'))[0] as any).Properties.EnvironmentVariables;
    // On (the default) spends no bytes of the V2 environment budget.
    expect(runtimeEnv(withFolders)).not.toHaveProperty('FOLDER_ATTACHMENTS_ENABLED');
    expect(runtimeEnv(docsOnly).FOLDER_ATTACHMENTS_ENABLED).toBe('false');
    const { FOLDER_ATTACHMENTS_ENABLED: _flag, ...rest } = runtimeEnv(docsOnly);
    expect(rest).toEqual(runtimeEnv(withFolders));
  });

  it('adds no resource', () => {
    const count = (t: Template) => Object.keys(t.toJSON().Resources).length;
    expect(count(withFolders)).toBe(count(docsOnly));
  });
});
