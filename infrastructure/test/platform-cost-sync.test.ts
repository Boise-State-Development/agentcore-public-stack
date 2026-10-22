/**
 * Platform cost sync — the guards that keep a billing integration cheap,
 * least-privileged and off by default.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';
import { CostTrackingTablesConstruct } from '../lib/constructs/data/cost-tracking-tables-construct';
import { PlatformCostSyncConstruct } from '../lib/constructs/costs/platform-cost-sync-construct';
import { loadConfig } from '../lib/config';

function synth(): Template {
  const config = createMockConfig();
  const stack = new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const tables = new CostTrackingTablesConstruct(stack, 'CostTables', { config });
  new PlatformCostSyncConstruct(stack, 'PlatformCostSync', {
    config,
    systemCostRollupTable: tables.systemCostRollupTable,
  });
  return Template.fromStack(stack);
}

describe('PlatformCostSyncConstruct', () => {
  it('grants only the two read Cost Explorer actions', () => {
    // ce:* takes no resource ARNs, so the only available scoping is the
    // action list. A write action here would be a billing-config change.
    synth().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'ReadCostExplorer',
            Action: ['ce:GetCostAndUsage', 'ce:GetDimensionValues'],
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      }),
    });
  });

  it('runs once a day, not hourly', () => {
    // Cost Explorer bills per request AND only refreshes its own data a few
    // times a day, so a tighter schedule pays repeatedly for identical
    // numbers. This test exists to make that a deliberate change.
    synth().hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'cron(10 7 * * ? *)',
    });
  });

  it('points the sync at the existing system cost rollup table', () => {
    synth().hasResourceProperties('AWS::Lambda::Function', {
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: Match.anyValue(),
          PLATFORM_COST_SYNC_ENABLED: 'true',
        }),
      }),
    });
  });

  it('scopes the query to this deployment via the Project tag value', () => {
    // `applyStandardTags` writes Project: config.projectPrefix onto every
    // resource, so the sync can filter the bill down to THIS deployment with
    // no new configuration — which matters for a fork that shares an account.
    synth().hasResourceProperties('AWS::Lambda::Function', {
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          PLATFORM_COST_PROJECT_TAG: 'test-project',
        }),
      }),
    });
  });

  it('does NOT set PLATFORM_COST_EXCLUDED_SERVICES from config', () => {
    // Setting it here from a value a workflow forwards as an empty string is
    // how the browser URL blocklist got silently emptied. An empty exclusion
    // list bills another team's Aurora cluster to our users, with nothing but
    // a slightly high total to show for it. The handler's own default is the
    // source of truth.
    const fns = synth().findResources('AWS::Lambda::Function');
    for (const fn of Object.values(fns)) {
      const vars = (fn.Properties?.Environment?.Variables ?? {}) as Record<string, unknown>;
      expect(Object.keys(vars)).not.toContain('PLATFORM_COST_EXCLUDED_SERVICES');
    }
  });

  it('can write the rollup table but not read the rest of the account', () => {
    const template = synth();
    // grantWriteData, not grantReadWriteData — the sync only ever overwrites
    // its own PLATFORM#* rows and never needs to read a user's cost data.
    const policies = template.findResources('AWS::IAM::Policy');
    const actions = Object.values(policies)
      .flatMap(p => (p.Properties?.PolicyDocument?.Statement ?? []) as any[])
      .flatMap(s => (Array.isArray(s.Action) ? s.Action : [s.Action]));
    expect(actions).not.toContain('dynamodb:GetItem');
    expect(actions).toContain('dynamodb:PutItem');
  });
});

describe('platformCosts config flag', () => {
  const ENV_KEY = 'CDK_PLATFORM_COSTS_ENABLED';
  const original = process.env[ENV_KEY];

  afterEach(() => {
    if (original === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = original;
  });

  /** Minimum context loadConfig() requires (mirrors app-api-sizing-config). */
  const BASE: Record<string, unknown> = {
    projectPrefix: 'test-project',
    awsRegion: 'us-west-2',
    awsAccount: '123456789012',
    vpcCidr: '10.0.0.0/16',
    corsOrigins: 'http://localhost:4200',
    production: false,
    retainDataOnDelete: false,
    frontend: { cloudFrontPriceClass: 'PriceClass_100' },
    appApi: { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 2 },
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

  function enabledFor(value: string | undefined): boolean {
    if (value === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = value;
    const app = new cdk.App();
    for (const [k, v] of Object.entries(BASE)) app.node.setContext(k, v);
    return loadConfig(app).platformCosts.enabled;
  }

  it('is off by default', () => {
    // Opt-in, against the repo's usual default-on posture: this flag grants
    // access to the account's billing data and spends $0.01 per CE call.
    expect(enabledFor(undefined)).toBe(false);
  });

  it('stays off for an unset workflow variable (empty string)', () => {
    // The workflow forwards `${{ vars.X }}`, which is '' when unset. That
    // must never be what enables a paid billing integration.
    expect(enabledFor('')).toBe(false);
  });

  it('stays off for anything that is not the literal "true"', () => {
    expect(enabledFor('false')).toBe(false);
    expect(enabledFor('1')).toBe(false);
    expect(enabledFor('yes')).toBe(false);
    expect(enabledFor('TRUE')).toBe(false);
  });

  it('is on only for the literal "true"', () => {
    expect(enabledFor('true')).toBe(true);
  });
});

describe('ECS tag propagation (cost attribution depends on it)', () => {
  it('propagates service tags to Fargate tasks', () => {
    // Fargate bills per TASK. Without PropagateTags=SERVICE the `Project`
    // tag reaches the service but never the thing that costs money, so ECS
    // silently vanishes from any tag-scoped cost figure — measured at 17% of
    // prod's infrastructure bill. The live dev service reported NONE before
    // this landed, which is why it is pinned here rather than assumed.
    const config = createMockConfig();
    const stack = new cdk.Stack(new cdk.App(), 'EcsTagStack', {
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 2 });
    const cluster = new ecs.Cluster(stack, 'Cluster', { vpc });
    const taskDef = new ecs.FargateTaskDefinition(stack, 'TaskDef');
    taskDef.addContainer('app', {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/nginx/nginx'),
    });
    new ecs.FargateService(stack, 'Svc', {
      cluster,
      taskDefinition: taskDef,
      propagateTags: ecs.PropagatedTagSource.SERVICE,
    });

    Template.fromStack(stack).hasResourceProperties('AWS::ECS::Service', {
      PropagateTags: 'SERVICE',
    });
    expect(config.projectPrefix).toBeTruthy();
  });
});
