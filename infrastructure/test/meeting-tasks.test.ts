/**
 * Meeting transcripts → GitHub issues (backend/src/agents/local_tools/meeting_tasks.py).
 * Pinned:
 *   - CDK_MEETING_TASKS_ENABLED is opt-in ('' and junk are off);
 *   - the Runtime gets MEETING_TASKS_ENABLED only while on, so an environment
 *     that leaves it off spends none of the V2 environment budget;
 *   - turning it on changes no IAM and adds no resource: the tools call vendor
 *     APIs with tokens from the AgentCore Identity vault the Runtime already
 *     reads for OAuth-gated MCP tools.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_MEETING_TASKS_ENABLED';

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

function resourceCount(template: Template): number {
  return Object.keys(template.toJSON().Resources).length;
}

describe('meetingTasks config', () => {
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
    expect(load().meetingTasks.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().meetingTasks.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().meetingTasks.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ meetingTasks: { enabled: true } }).meetingTasks.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ meetingTasks: { enabled: true } }).meetingTasks.enabled).toBe(false);
  });
});

describe('meeting tasks wiring', () => {
  const off = synth();
  const on = synth({ meetingTasks: { enabled: true } });

  it('spends no Runtime env bytes while off, and sets the flag only while on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('MEETING_TASKS_ENABLED');
    expect(runtimeEnv(on).MEETING_TASKS_ENABLED).toBe('true');
  });

  it('never reaches app-api: the tools are built on the invocation path only', () => {
    const taskDefs = Object.values(on.findResources('AWS::ECS::TaskDefinition'));
    expect(JSON.stringify(taskDefs)).not.toContain('MEETING_TASKS_ENABLED');
  });

  it('changes no IAM when turned on', () => {
    const policies = (t: Template) =>
      ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].map((type) => JSON.stringify(t.findResources(type)));
    expect(policies(on)).toEqual(policies(off));
  });

  it('adds no resource', () => {
    expect(resourceCount(on)).toBe(resourceCount(off));
  });
});
