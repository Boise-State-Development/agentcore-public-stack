// Guards the appApi sizing precedence chain: env var > flat dotted context > nested
// context object. The flat-key form is the trap — `--context appApi.cpu=2048`
// (what scripts/common/load-env.sh emits) sets context['appApi.cpu'], NOT a nested
// object, so reading only the nested form accepts the operator's flag and ignores it.
// Case 4 covers the GitHub-Actions case where an unset `vars.*` arrives as an
// empty string and must fall through to the committed default rather than
// becoming 0 or NaN.
import * as cdk from 'aws-cdk-lib';
import { API_CONVERSE_MAX_IN_FLIGHT_DEFAULT, loadConfig } from '../lib/config';

const BASE: Record<string, unknown> = {
  projectPrefix: 'test-project',
  awsRegion: 'us-west-2',
  awsAccount: '123456789012',
  vpcCidr: '10.0.0.0/16',
  corsOrigins: 'http://localhost:4200',
  production: false,
  retainDataOnDelete: false,
  frontend: { cloudFrontPriceClass: 'PriceClass_100' },
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

function mk(ctx: Record<string, unknown>) {
  const app = new cdk.App();
  for (const [k, v] of Object.entries({ ...BASE, ...ctx })) app.node.setContext(k, v);
  return loadConfig(app);
}

describe('appApi sizing precedence', () => {
  const saved = { ...process.env };
  afterEach(() => { process.env = { ...saved }; });

  it('1. nested context object (the committed cdk.context.json default)', () => {
    const c = mk({ appApi: { cpu: 1024, memory: 2048, desiredCount: 2, maxCapacity: 10 } });
    expect([c.appApi.cpu, c.appApi.memory, c.appApi.desiredCount]).toEqual([1024, 2048, 2]);
  });

  it('2. FLAT dotted context beats nested (this was silently ignored before)', () => {
    const c = mk({
      appApi: { cpu: 1024, memory: 2048, desiredCount: 2, maxCapacity: 10 },
      'appApi.cpu': '2048', 'appApi.memory': '4096', 'appApi.desiredCount': '3',
    });
    expect([c.appApi.cpu, c.appApi.memory, c.appApi.desiredCount]).toEqual([2048, 4096, 3]);
  });

  it('3. env var beats both', () => {
    process.env.CDK_APP_API_CPU = '4096';
    process.env.CDK_APP_API_MEMORY = '8192';
    const c = mk({
      appApi: { cpu: 1024, memory: 2048, desiredCount: 2, maxCapacity: 10 },
      'appApi.cpu': '2048', 'appApi.memory': '4096',
    });
    expect([c.appApi.cpu, c.appApi.memory]).toEqual([4096, 8192]);
  });

  it('4. an UNSET GitHub variable ("") falls through to the committed default', () => {
    process.env.CDK_APP_API_CPU = '';
    process.env.CDK_APP_API_MEMORY = '';
    process.env.CDK_APP_API_DESIRED_COUNT = '';
    process.env.CDK_APP_API_MAX_CAPACITY = '';
    const c = mk({ appApi: { cpu: 1024, memory: 2048, desiredCount: 2, maxCapacity: 10 } });
    expect([c.appApi.cpu, c.appApi.memory, c.appApi.desiredCount, c.appApi.maxCapacity])
      .toEqual([1024, 2048, 2, 10]);
  });
});

describe('appApi.apiConverseMaxInFlight', () => {
  const saved = { ...process.env };
  const sized = { appApi: { cpu: 1024, memory: 2048, desiredCount: 2, maxCapacity: 10 } };
  afterEach(() => { process.env = { ...saved }; });

  it('defaults to the committed constant when nothing sets it', () => {
    delete process.env.CDK_APP_API_CONVERSE_MAX_IN_FLIGHT;
    expect(mk(sized).appApi.apiConverseMaxInFlight).toBe(API_CONVERSE_MAX_IN_FLIGHT_DEFAULT);
  });

  it('an unset GitHub variable arrives as "" and falls through to the default', () => {
    process.env.CDK_APP_API_CONVERSE_MAX_IN_FLIGHT = '';
    expect(mk(sized).appApi.apiConverseMaxInFlight).toBe(API_CONVERSE_MAX_IN_FLIGHT_DEFAULT);
  });

  it('env var beats flat context beats nested context', () => {
    expect(mk({ ...sized, appApi: { ...sized.appApi, apiConverseMaxInFlight: 4 } })
      .appApi.apiConverseMaxInFlight).toBe(4);
    expect(mk({ ...sized, appApi: { ...sized.appApi, apiConverseMaxInFlight: 4 },
      'appApi.apiConverseMaxInFlight': '8' }).appApi.apiConverseMaxInFlight).toBe(8);
    process.env.CDK_APP_API_CONVERSE_MAX_IN_FLIGHT = '32';
    expect(mk({ ...sized, 'appApi.apiConverseMaxInFlight': '8' }).appApi.apiConverseMaxInFlight).toBe(32);
  });

  it('rejects 0 rather than shipping a cap that refuses every call', () => {
    process.env.CDK_APP_API_CONVERSE_MAX_IN_FLIGHT = '0';
    expect(() => mk(sized)).toThrow(/apiConverseMaxInFlight/);
  });

  it('rejects a value that is not a whole number instead of truncating it', () => {
    process.env.CDK_APP_API_CONVERSE_MAX_IN_FLIGHT = '8.5';
    expect(() => mk(sized)).toThrow(/CDK_APP_API_CONVERSE_MAX_IN_FLIGHT/);
  });
});
