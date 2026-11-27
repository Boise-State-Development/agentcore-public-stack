// AgentCore Runtime V2 build warmup hook (docs/specs/agentcore-runtime-v2.md §5b).
//
// Three things are guarded. The switch: in development, so only the literal
// `true` turns it on and an unset GitHub variable ('') is off. The template:
// `HooksConfiguration` is on the Runtime only when the switch is on AND the
// platform version is V2, because the service rejects hooks on V1 with a
// ValidationException. And the route: the path CloudFormation is given is the
// one the container serves.
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { AGENTCORE_RUNTIME_WARMUP_HOOK_PATH, loadConfig } from '../lib/config';
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

describe('inferenceApi.runtimeWarmupHookEnabled', () => {
  const saved = { ...process.env };
  afterEach(() => { process.env = { ...saved }; });

  it('is off by default', () => {
    delete process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED;
    expect(mk().inferenceApi.runtimeWarmupHookEnabled).toBe(false);
  });

  it('an unset GitHub variable arrives as "" and is off', () => {
    process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED = '';
    expect(mk().inferenceApi.runtimeWarmupHookEnabled).toBe(false);
  });

  it.each(['true', 'TRUE', ' True '])('%p turns it on', (value) => {
    process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED = value;
    expect(mk().inferenceApi.runtimeWarmupHookEnabled).toBe(true);
  });

  it.each(['false', '1', 'yes', 'on'])('%p leaves it off: only the literal true enables', (value) => {
    process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED = value;
    expect(mk().inferenceApi.runtimeWarmupHookEnabled).toBe(false);
  });

  it('env var beats flat context beats nested context', () => {
    delete process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED;
    expect(mk({ inferenceApi: { runtimeWarmupHookEnabled: true } })
      .inferenceApi.runtimeWarmupHookEnabled).toBe(true);
    expect(mk({ inferenceApi: { runtimeWarmupHookEnabled: true }, 'inferenceApi.runtimeWarmupHookEnabled': false })
      .inferenceApi.runtimeWarmupHookEnabled).toBe(false);
    process.env.CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED = 'false';
    expect(mk({ 'inferenceApi.runtimeWarmupHookEnabled': true }).inferenceApi.runtimeWarmupHookEnabled).toBe(false);
  });
});

describe('AgentCore Runtime HooksConfiguration property', () => {
  function synth(inferenceApi: { runtimePlatformVersion: string; runtimeWarmupHookEnabled?: boolean }) {
    const config = createMockConfig({ inferenceApi });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    const runtimes = Object.values(Template.fromStack(stack).findResources('AWS::BedrockAgentCore::Runtime'));
    expect(runtimes).toHaveLength(1);
    return { stack, props: runtimes[0].Properties as Record<string, unknown> };
  }

  it('V2 with the switch on configures the build warmup hook on the serving port', () => {
    const { props } = synth({ runtimePlatformVersion: 'V2', runtimeWarmupHookEnabled: true });
    // No Port: AgentCore then calls the Runtime's serving port, which is the
    // only server in the container.
    expect(props.HooksConfiguration).toEqual({
      Build: { Warmup: { Action: { HttpPost: { Path: '/warmup' } } } },
    });
  });

  it('V1 with the switch on configures nothing and warns: the service rejects hooks on V1', () => {
    const { stack, props } = synth({ runtimePlatformVersion: 'V1', runtimeWarmupHookEnabled: true });
    expect(props).not.toHaveProperty('HooksConfiguration');
    Annotations.fromStack(stack).hasWarning(
      '*',
      Match.stringLikeRegexp('CDK_AGENTCORE_RUNTIME_WARMUP_HOOK_ENABLED is true but the Runtime platform version is V1'),
    );
  });

  it.each(['V1', 'V2'])('%s with the switch off carries no hook, so an update removes one', (version) => {
    const { stack, props } = synth({ runtimePlatformVersion: version, runtimeWarmupHookEnabled: false });
    expect(props).not.toHaveProperty('HooksConfiguration');
    Annotations.fromStack(stack).hasNoWarning('*', Match.stringLikeRegexp('WARMUP_HOOK'));
  });

  it('the default configuration carries no hook', () => {
    expect(synth({ runtimePlatformVersion: 'V2' }).props).not.toHaveProperty('HooksConfiguration');
  });

  it('never combines hooks with a capacity provider, which the service rejects', () => {
    const { props } = synth({ runtimePlatformVersion: 'V2', runtimeWarmupHookEnabled: true });
    expect(props).not.toHaveProperty('CapacityProviderConfiguration');
  });
});

describe('the hook path', () => {
  it('is the route the inference-api container serves', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '../../backend/src/apis/inference_api/warmup_hook.py'),
      'utf8',
    );
    const match = source.match(/^WARMUP_HOOK_PATH = "([^"]+)"$/m);
    expect(match).not.toBeNull();
    expect(match![1]).toBe(AGENTCORE_RUNTIME_WARMUP_HOOK_PATH);
  });

  it('meets the CloudFormation schema and is not a route AgentCore already calls', () => {
    expect(AGENTCORE_RUNTIME_WARMUP_HOOK_PATH).toMatch(/^\/[A-Za-z0-9._~/-]*$/);
    expect(AGENTCORE_RUNTIME_WARMUP_HOOK_PATH.length).toBeLessThanOrEqual(32);
    expect(['/invocations', '/ping', '/ws']).not.toContain(AGENTCORE_RUNTIME_WARMUP_HOOK_PATH);
  });
});
