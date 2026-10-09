// AgentCore Runtime platform version (docs/specs/agentcore-runtime-v2.md).
//
// Two things are guarded. The config chain: env var > flat dotted context >
// nested context > V1, with an unset GitHub variable ('') falling through and
// a typo failing synth rather than reaching the service, whose CloudFormation
// schema takes any non-blank string. And the template: `PlatformVersion` is
// on the Runtime for V1 as well as V2, because an omitted property would make
// a V2 -> V1 rollback a removal whose effect is the service's call.
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AGENTCORE_RUNTIME_PLATFORM_VERSION_DEFAULT, loadConfig } from '../lib/config';
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

function mk(ctx: Record<string, unknown> = {}) {
  const app = new cdk.App();
  for (const [k, v] of Object.entries({ ...BASE, ...ctx })) app.node.setContext(k, v);
  return loadConfig(app);
}

describe('inferenceApi.runtimePlatformVersion', () => {
  const saved = { ...process.env };
  afterEach(() => { process.env = { ...saved }; });

  it('defaults to V1, so an existing stack is unchanged', () => {
    delete process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION;
    expect(AGENTCORE_RUNTIME_PLATFORM_VERSION_DEFAULT).toBe('V1');
    expect(mk().inferenceApi.runtimePlatformVersion).toBe('V1');
  });

  it('an unset GitHub variable arrives as "" and falls through to the default', () => {
    process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION = '';
    expect(mk().inferenceApi.runtimePlatformVersion).toBe('V1');
  });

  it('env var beats flat context beats nested context', () => {
    delete process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION;
    expect(mk({ inferenceApi: { runtimePlatformVersion: 'V2' } })
      .inferenceApi.runtimePlatformVersion).toBe('V2');
    expect(mk({ inferenceApi: { runtimePlatformVersion: 'V1' }, 'inferenceApi.runtimePlatformVersion': 'V2' })
      .inferenceApi.runtimePlatformVersion).toBe('V2');
    process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION = 'V1';
    expect(mk({ 'inferenceApi.runtimePlatformVersion': 'V2' }).inferenceApi.runtimePlatformVersion).toBe('V1');
  });

  it('trims surrounding whitespace from the variable', () => {
    process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION = ' V2 ';
    expect(mk().inferenceApi.runtimePlatformVersion).toBe('V2');
  });

  it.each(['v2', '2', 'V3', 'latest'])('rejects %p instead of passing it to the service', (value) => {
    process.env.CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION = value;
    expect(() => mk()).toThrow(/CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION/);
  });
});

describe('AgentCore Runtime PlatformVersion property', () => {
  function synthRuntimeProps(version: string): Record<string, unknown> {
    const config = createMockConfig({ inferenceApi: { runtimePlatformVersion: version } });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    const runtimes = Object.values(Template.fromStack(stack).findResources('AWS::BedrockAgentCore::Runtime'));
    expect(runtimes).toHaveLength(1);
    return runtimes[0].Properties as Record<string, unknown>;
  }

  it.each(['V1', 'V2'])('writes PlatformVersion %s explicitly', (version) => {
    expect(synthRuntimeProps(version).PlatformVersion).toBe(version);
  });
});
