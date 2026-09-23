/**
 * Shared Projects storage and wiring (docs/specs/shared-projects.md §3.1, PR-1.1).
 *
 * Two contracts live here that nothing else checks:
 *
 *   1. The table name. inference-api derives it as `${PROJECT_PREFIX}-projects`
 *      instead of reading `DYNAMODB_PROJECTS_TABLE_NAME`, because the AgentCore
 *      Runtime is capped at 50 env vars (runtime-env-var-limit.test.ts). If the
 *      construct ever names the table differently, every membership check on the
 *      invocation path hits a missing table — and only in the cloud.
 *
 *   2. The runtime's grant is read + update only. Project rows are created and
 *      deleted by app-api's CRUD surface; the invocation path resolves
 *      membership, back-fills a member's userId and bumps COST# rollups.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { ProjectsConstruct } from '../lib/constructs/data/projects-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('ProjectsConstruct', () => {
  const config = createMockConfig();
  let t: Template;

  beforeAll(() => {
    const stack = new cdk.Stack(new cdk.App(), 'TestStack', {
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    new ProjectsConstruct(stack, 'Projects', { config });
    t = Template.fromStack(stack);
  });

  it('names the table `${projectPrefix}-projects` — the name inference-api derives', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: `${config.projectPrefix}-projects`,
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  it('carries OwnerIndex and MemberIndex, created with the table', () => {
    const tables = t.findResources('AWS::DynamoDB::Table');
    const [table] = Object.values(tables);
    const indexes = (table as any).Properties.GlobalSecondaryIndexes.map((g: any) => ({
      name: g.IndexName,
      keys: g.KeySchema.map((k: any) => k.AttributeName),
      projection: g.Projection.ProjectionType,
    }));
    expect(indexes).toEqual([
      { name: 'OwnerIndex', keys: ['GSI1PK', 'GSI1SK'], projection: 'ALL' },
      { name: 'MemberIndex', keys: ['GSI2PK', 'GSI2SK'], projection: 'ALL' },
    ]);
  });

  it('expires notification rows on `ttl` and keeps point-in-time recovery', () => {
    t.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: `${config.projectPrefix}-projects`,
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });
});

/**
 * Both compute roles are past the inline-policy size limit, so CDK spills newer
 * statements into an `OverflowPolicy` managed policy. Search both kinds.
 */
function findStatement(template: Template, sid: string): any {
  return ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']
    .flatMap((type) => Object.values(template.findResources(type)))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
    .find((s: any) => s.Sid === sid);
}

describe('Shared Projects compute wiring', () => {
  let template: Template;
  let runtimeEnv: Record<string, unknown>;

  beforeAll(() => {
    const config = createMockConfig({ projects: { enabled: true } });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    runtimeEnv = (Object.values(runtimes)[0] as any).Properties.EnvironmentVariables;
  });

  it('gives the Runtime the kill switch and the prefix, not a table-name variable', () => {
    expect(runtimeEnv.PROJECTS_ENABLED).toBe('true');
    expect(runtimeEnv.PROJECT_PREFIX).toBeDefined();
    expect(runtimeEnv).not.toHaveProperty('DYNAMODB_PROJECTS_TABLE_NAME');
  });

  it('grants the Runtime read + update on the table and its indexes, never put or delete', () => {
    const statement = findStatement(template, 'ProjectsTableReadUpdate');

    expect(statement).toBeDefined();
    expect([...statement.Action].sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]);
    expect(statement.Resource).toHaveLength(2);
  });

  it('gives app-api the table name, the flag and full CRUD on the table', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: 'PROJECTS_ENABLED', Value: 'true' },
            Match.objectLike({ Name: 'DYNAMODB_PROJECTS_TABLE_NAME' }),
          ]),
        }),
      ]),
    });
    const statement = findStatement(template, 'ProjectsTableAccess');
    expect(statement.Action).toEqual(
      expect.arrayContaining(['dynamodb:PutItem', 'dynamodb:DeleteItem', 'dynamodb:Query']),
    );
    expect(statement.Resource).toHaveLength(2);
  });
});
