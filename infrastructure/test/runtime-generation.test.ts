// AgentCore Runtime generation (CDK_AGENTCORE_RUNTIME_GENERATION).
//
// A Runtime stops accepting updates at its account's "Versions per Agent"
// quota (1,000 by default), and AWS cannot delete a single version, so the way
// back is a new Runtime. CloudFormation cannot replace one under the same
// name; a generation suffix gives it a new name, and CloudFormation creates
// the new Runtime, repoints every reference, then deletes the old one.
//
// Two things are guarded. Unset (including '', which is what an unset GitHub
// variable arrives as) changes nothing: the name is the original one. Set, the
// template differs from the unset one ONLY by the name, so every reference
// (the runtime-id SSM parameter, app-api's INFERENCE_API_URL, the scheduled-
// runs Lambdas, alarms and the dashboard's `Name` dimension, the log-retention
// sweep) follows the new Runtime instead of pointing at the deleted one.
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { getAgentRuntimeName, loadConfig } from '../lib/config';
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

const BASE_NAME = 'test_project_agentcore_runtime';

describe('inferenceApi.runtimeGeneration', () => {
  const saved = { ...process.env };
  afterEach(() => { process.env = { ...saved }; });

  it('is unset by default, so the Runtime keeps its original name', () => {
    delete process.env.CDK_AGENTCORE_RUNTIME_GENERATION;
    const config = mk();
    expect(config.inferenceApi.runtimeGeneration).toBeUndefined();
    expect(getAgentRuntimeName(config)).toBe(BASE_NAME);
  });

  it('an unset GitHub variable arrives as "" and falls through to unset', () => {
    process.env.CDK_AGENTCORE_RUNTIME_GENERATION = '';
    expect(mk().inferenceApi.runtimeGeneration).toBeUndefined();
  });

  it('suffixes the name, from the variable (trimmed) or context', () => {
    process.env.CDK_AGENTCORE_RUNTIME_GENERATION = ' g2 ';
    expect(getAgentRuntimeName(mk())).toBe(`${BASE_NAME}_g2`);
    delete process.env.CDK_AGENTCORE_RUNTIME_GENERATION;
    expect(getAgentRuntimeName(mk({ 'inferenceApi.runtimeGeneration': '3' }))).toBe(`${BASE_NAME}_3`);
  });

  it.each(['G2', 'g-2', 'g_2', 'toolonggen'])('rejects %p, which is not a short lowercase alphanumeric', (value) => {
    process.env.CDK_AGENTCORE_RUNTIME_GENERATION = value;
    expect(() => mk()).toThrow(/CDK_AGENTCORE_RUNTIME_GENERATION/);
  });

  it('fits AgentCore\'s 48-character name limit at the longest prefix and generation', () => {
    process.env.CDK_AGENTCORE_RUNTIME_GENERATION = 'abcdefgh';
    const name = getAgentRuntimeName(mk({ projectPrefix: 'a23456789012345678901' }));
    expect(name).toMatch(/^[a-zA-Z][a-zA-Z0-9_]{0,47}$/);
    expect(name).toHaveLength(48);
  });
});

describe('AgentCore Runtime under a generation', () => {
  function synth(runtimeGeneration?: string): Template {
    const config = createMockConfig({
      inferenceApi: { runtimePlatformVersion: 'V2', ...(runtimeGeneration ? { runtimeGeneration } : {}) },
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    return Template.fromStack(stack);
  }

  let unset: Template;
  let g2: Template;
  beforeAll(() => {
    unset = synth();
    g2 = synth('g2');
  });

  function runtimeOf(template: Template): [string, Record<string, unknown>] {
    const runtimes = Object.entries(template.findResources('AWS::BedrockAgentCore::Runtime'));
    expect(runtimes).toHaveLength(1);
    return [runtimes[0][0], runtimes[0][1].Properties as Record<string, unknown>];
  }

  it('unset keeps the original Runtime name', () => {
    expect(runtimeOf(unset)[1].AgentRuntimeName).toBe(BASE_NAME);
  });

  it('set renames the same logical resource, so CloudFormation replaces it rather than adding a second', () => {
    const [unsetId] = runtimeOf(unset);
    const [g2Id, props] = runtimeOf(g2);
    expect(g2Id).toBe(unsetId);
    expect(props.AgentRuntimeName).toBe(`${BASE_NAME}_g2`);
  });

  it('differs from the unset template only by the Runtime name, so every reference follows it', () => {
    // RagCorsUpdater stamps Date.now() on purpose, to re-run on every deploy;
    // it is not the Runtime's and differs between any two synths.
    const normalize = (json: string) => json.replace(/"Timestamp":"\d+"/g, '"Timestamp":"0"');
    const renamed = JSON.stringify(g2.toJSON()).split(`${BASE_NAME}_g2`).join(BASE_NAME);
    expect(JSON.parse(normalize(renamed))).toEqual(JSON.parse(normalize(JSON.stringify(unset.toJSON()))));
  });

  it('points the runtime-id parameter and app-api at the new Runtime by reference', () => {
    const [runtimeId] = runtimeOf(g2);
    g2.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/inference-api/runtime-id',
      Value: { 'Fn::GetAtt': [runtimeId, 'AgentRuntimeId'] },
    });
    const json = JSON.stringify(g2.toJSON());
    expect(json).toContain(`"${runtimeId}","AgentRuntimeArn"`);
  });

  it('moves the metric dimension and the log-retention sweep to the new name', () => {
    const json = JSON.stringify(g2.toJSON());
    expect(json).toContain(`${BASE_NAME}_g2::DEFAULT`);
    expect(json).not.toContain(`${BASE_NAME}::DEFAULT`);
    g2.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: { LOG_GROUP_PREFIX: `/aws/bedrock-agentcore/runtimes/${BASE_NAME}_g2-` },
      },
    });
  });
});
