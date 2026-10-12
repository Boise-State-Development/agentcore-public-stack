/**
 * Skill authoring tools (docs/specs/skill-creator.md D12). Pinned:
 *   - CDK_SKILL_AUTHORING_ENABLED is opt-in ('' and junk are off);
 *   - the Runtime gets SKILL_AUTHORING_ENABLED only while on, so an environment
 *     that leaves it off spends none of the V2 environment budget on it, and
 *     app-api never gets it;
 *   - the Runtime can put and delete objects only under the skill-resources
 *     bucket's `skills/` prefix, and the read statement is unchanged. Pinned
 *     versions' frozen copies (`skill-versions/`) stay out of reach.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_SKILL_AUTHORING_ENABLED';

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

function statementsWithSid(template: Template, sid: string): any[] {
  const found: any[] = [];
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy')) as any[]) {
    for (const stmt of policy.Properties.PolicyDocument.Statement) {
      if (stmt.Sid === sid) found.push(stmt);
    }
  }
  return found;
}

describe('skillAuthoring config', () => {
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
    expect(load().skillAuthoring.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().skillAuthoring.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().skillAuthoring.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ skillAuthoring: { enabled: true } }).skillAuthoring.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ skillAuthoring: { enabled: true } }).skillAuthoring.enabled).toBe(false);
  });
});

describe('skill authoring wiring', () => {
  it('spends no Runtime env bytes while off, and sets the flag only while on', () => {
    expect(runtimeEnv(synth())).not.toHaveProperty('SKILL_AUTHORING_ENABLED');
    expect(runtimeEnv(synth({ skillAuthoring: { enabled: true } })).SKILL_AUTHORING_ENABLED).toBe('true');
  });

  it('never reaches app-api', () => {
    expect(appApiEnv(synth({ skillAuthoring: { enabled: true } }))).not.toHaveProperty('SKILL_AUTHORING_ENABLED');
  });

  it('lets the Runtime put and delete only under skills/, whatever the flag says', () => {
    for (const template of [synth(), synth({ skillAuthoring: { enabled: true } })]) {
      const [write] = statementsWithSid(template, 'SkillResourcesBucketWrite');
      expect(write.Action).toEqual(['s3:PutObject', 's3:DeleteObject']);
      const resource = JSON.stringify(write.Resource);
      expect(resource).toContain('/skills/*');
      expect(resource).not.toContain('skill-versions');
    }
  });

  it('leaves the read statement as it was', () => {
    const [read] = statementsWithSid(synth(), 'SkillResourcesBucketRead');
    expect(read.Action).toEqual(['s3:GetObject', 's3:ListBucket']);
  });
});
