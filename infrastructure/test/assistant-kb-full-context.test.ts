/**
 * Assistant KB full-context mode (backend/src/apis/shared/assistants/kb_full_context.py).
 * Pinned:
 *   - CDK_ASSISTANT_KB_FULL_CONTEXT_ENABLED is opt-in ('' and junk are off);
 *   - the Runtime gets ASSISTANT_KB_FULL_CONTEXT_ENABLED only while on, so an
 *     environment that leaves it off spends none of the V2 environment budget;
 *   - the read it needs on the documents bucket's `assistants/` prefix is the
 *     Runtime's standing `AssistantDocumentsRead` grant (spreadsheet analysis
 *     reads the same uploads), so turning the flag on changes no IAM and adds
 *     no resource.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_ASSISTANT_KB_FULL_CONTEXT_ENABLED';
const BUDGET_ENV = 'CDK_ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS';
const KNOWLEDGE_FILES_ENV = 'CDK_AGENT_KNOWLEDGE_FILES_ENABLED';

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

function appApiEnv(template: Template): Record<string, string> {
  const taskDefs = Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[];
  const container = taskDefs
    .flatMap((t) => t.Properties.ContainerDefinitions)
    .find((c: any) => (c.Environment ?? []).some((e: any) => e.Name === 'DYNAMODB_ASSISTANTS_TABLE_NAME'));
  expect(container).toBeDefined();
  return Object.fromEntries(container.Environment.map((e: any) => [e.Name, e.Value]));
}

function documentsRead(template: Template): any | undefined {
  return ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']
    .flatMap((type) => Object.values(template.findResources(type)))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
    .find((s: any) => s.Sid === 'AssistantDocumentsRead');
}

function resourceCount(template: Template): number {
  return Object.keys(template.toJSON().Resources).length;
}

describe('assistantKbFullContext config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[FLAG_ENV];
    delete process.env[BUDGET_ENV];
    delete process.env[KNOWLEDGE_FILES_ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().assistantKbFullContext.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().assistantKbFullContext.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().assistantKbFullContext.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ assistantKbFullContext: { enabled: true } }).assistantKbFullContext.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ assistantKbFullContext: { enabled: true } }).assistantKbFullContext.enabled).toBe(false);
  });

  it('budgets 50,000 tokens unless told otherwise, and "" is the default', () => {
    expect(load().assistantKbFullContext.maxTokens).toBe(50_000);
    process.env[BUDGET_ENV] = '';
    expect(load().assistantKbFullContext.maxTokens).toBe(50_000);
    process.env[BUDGET_ENV] = '80000';
    expect(load().assistantKbFullContext.maxTokens).toBe(80_000);
    process.env[BUDGET_ENV] = '0';
    expect(load().assistantKbFullContext.maxTokens).toBe(0);
    delete process.env[BUDGET_ENV];
    expect(load({ assistantKbFullContext: { maxTokens: 30000 } }).assistantKbFullContext.maxTokens).toBe(30_000);
  });

  it.each(['50k', '-1', '1.5', 'lots'])('refuses a budget of %j rather than guess', (value) => {
    process.env[BUDGET_ENV] = value;
    expect(() => load()).toThrow(/CDK_ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS/);
  });
});

describe('agentKnowledgeFiles config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[KNOWLEDGE_FILES_ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().agentKnowledgeFiles.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1', 'on'])('treats %j as off', (value) => {
    process.env[KNOWLEDGE_FILES_ENV] = value;
    expect(load().agentKnowledgeFiles.enabled).toBe(false);
  });

  it.each(['true', ' True '])('turns on for %j', (value) => {
    process.env[KNOWLEDGE_FILES_ENV] = value;
    expect(load().agentKnowledgeFiles.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ agentKnowledgeFiles: { enabled: true } }).agentKnowledgeFiles.enabled).toBe(true);
    process.env[KNOWLEDGE_FILES_ENV] = 'false';
    expect(load({ agentKnowledgeFiles: { enabled: true } }).agentKnowledgeFiles.enabled).toBe(false);
  });
});

describe('assistant KB full-context wiring', () => {
  const off = synth();
  const on = synth({ assistantKbFullContext: { enabled: true, maxTokens: 50_000 } });

  it('spends no Runtime env bytes while off, and sets the flag only while on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('ASSISTANT_KB_FULL_CONTEXT_ENABLED');
    expect(runtimeEnv(on).ASSISTANT_KB_FULL_CONTEXT_ENABLED).toBe('true');
  });

  it('relies on the standing documents read, on or off', () => {
    for (const template of [off, on]) {
      const grant = documentsRead(template);
      expect(grant).toBeDefined();
      expect(grant.Action).toBe('s3:GetObject');
      expect(JSON.stringify(grant.Resource)).toContain('/assistants/*');
    }
  });

  it('changes no IAM when turned on', () => {
    const policies = (t: Template) =>
      ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].map((type) => JSON.stringify(t.findResources(type)));
    expect(policies(on)).toEqual(policies(off));
  });

  it('adds no resource: the stack is within a few of the 500 limit', () => {
    expect(resourceCount(on)).toBe(resourceCount(off));
  });

  it('writes the budget to the Runtime only while on and not the default', () => {
    const tuned = synth({ assistantKbFullContext: { enabled: true, maxTokens: 80_000 } });
    const tunedOff = synth({ assistantKbFullContext: { enabled: false, maxTokens: 80_000 } });
    expect(runtimeEnv(on)).not.toHaveProperty('ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS');
    expect(runtimeEnv(off)).not.toHaveProperty('ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS');
    expect(runtimeEnv(tunedOff)).not.toHaveProperty('ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS');
    expect(runtimeEnv(tuned).ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS).toBe('80000');
  });

  it("gives app-api the Runtime's flag and budget, for the Agent Designer's mode line", () => {
    const tuned = synth({ assistantKbFullContext: { enabled: true, maxTokens: 80_000 } });
    expect(appApiEnv(off)).toMatchObject({
      ASSISTANT_KB_FULL_CONTEXT_ENABLED: 'false',
      ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS: '50000',
    });
    expect(appApiEnv(tuned)).toMatchObject({
      ASSISTANT_KB_FULL_CONTEXT_ENABLED: 'true',
      ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS: '80000',
    });
  });
});

describe('knowledge_files wiring', () => {
  const off = synth();
  const on = synth({ agentKnowledgeFiles: { enabled: true } });

  it('spends no Runtime env bytes while off, and sets the flag only while on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('AGENT_KNOWLEDGE_FILES_ENABLED');
    expect(runtimeEnv(on).AGENT_KNOWLEDGE_FILES_ENABLED).toBe('true');
  });

  it('is Runtime only: app-api never builds the tool', () => {
    expect(appApiEnv(on)).not.toHaveProperty('AGENT_KNOWLEDGE_FILES_ENABLED');
  });

  it('changes no IAM and adds no resource', () => {
    const policies = (t: Template) =>
      ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].map((type) => JSON.stringify(t.findResources(type)));
    expect(policies(on)).toEqual(policies(off));
    expect(resourceCount(on)).toBe(resourceCount(off));
  });
});
