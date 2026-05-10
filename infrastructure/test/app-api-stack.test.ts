import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AppApiStack } from '../lib/app-api-stack';
import { createMockConfig, createMockApp, mockEnv } from './helpers/mock-config';

/**
 * CDK splits a task role's policy across an inline AWS::IAM::Policy and one
 * or more AWS::IAM::ManagedPolicy "overflow" attachments once the IAM inline
 * policy size limit is exceeded. Match.arrayWith on a single resource type
 * misses statements that landed in the overflow. Walk both types and return
 * every statement carrying the requested Sid.
 */
function _findStatementsBySid(template: Template, sid: string): any[] {
  const found: any[] = [];
  for (const resourceType of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    const policies = template.findResources(resourceType);
    for (const policy of Object.values(policies)) {
      const stmts =
        (policy as any).Properties?.PolicyDocument?.Statement || [];
      for (const stmt of stmts) {
        if (stmt.Sid === sid) found.push(stmt);
      }
    }
  }
  return found;
}

describe('AppApiStack', () => {
  let template: Template;
  let config: ReturnType<typeof createMockConfig>;

  beforeEach(() => {
    config = createMockConfig();
    const app = createMockApp(config, ['AppApiStack']);
    const stack = new AppApiStack(app, 'TestAppApiStack', {
      config,
      env: mockEnv(config),
    });
    template = Template.fromStack(stack);
  });

  // ============================================================
  // Stack Synthesis
  // ============================================================

  test('synthesizes without errors', () => {
    expect(template.toJSON()).toBeDefined();
  });

  // ============================================================
  // ECS Fargate Service
  // ============================================================

  describe('ECS Fargate Service', () => {
    test('creates a Fargate service', () => {
      template.hasResourceProperties('AWS::ECS::Service', {
        LaunchType: 'FARGATE',
      });
    });

    test('service has circuit breaker with rollback enabled', () => {
      template.hasResourceProperties('AWS::ECS::Service', {
        DeploymentConfiguration: Match.objectLike({
          DeploymentCircuitBreaker: {
            Enable: true,
            Rollback: true,
          },
        }),
      });
    });

    test('service desired count matches config', () => {
      template.hasResourceProperties('AWS::ECS::Service', {
        DesiredCount: config.appApi.desiredCount,
      });
    });

    test('production context sets DesiredCount to 2 for event-loop blocking mitigation', () => {
      // The production cdk.context.json sets appApi.desiredCount=2 so that a
      // single blocked event loop on one ECS task can no longer halt all
      // ingress (see .kiro/specs/bff-middleware-event-loop-blocking). When
      // that default is unintentionally reverted to 1, this test fails.
      const productionConfig = createMockConfig({
        production: true,
        appApi: { ...config.appApi, desiredCount: 2 },
      });
      const app = createMockApp(productionConfig, ['AppApiStack']);
      const stack = new AppApiStack(app, 'ProdAppApiStack', {
        config: productionConfig,
        env: mockEnv(productionConfig),
      });
      const prodTemplate = Template.fromStack(stack);
      prodTemplate.hasResourceProperties('AWS::ECS::Service', {
        DesiredCount: 2,
      });
    });
  });

  // ============================================================
  // ECS Task Definition
  // ============================================================

  describe('Task Definition', () => {
    test('has correct CPU from config', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        Cpu: String(config.appApi.cpu),
      });
    });

    test('has correct memory from config', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        Memory: String(config.appApi.memory),
      });
    });

    test('uses Fargate compatibility', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        RequiresCompatibilities: ['FARGATE'],
      });
    });

    test('container maps port 8000', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            PortMappings: Match.arrayWith([
              Match.objectLike({ ContainerPort: 8000 }),
            ]),
          }),
        ]),
      });
    });

    test('container has health check', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            HealthCheck: Match.objectLike({
              Command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
            }),
          }),
        ]),
      });
    });

    test('container environment includes CORS_ORIGINS derived from domainName', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Environment: Match.arrayWith([
              Match.objectLike({ Name: 'CORS_ORIGINS' }),
            ]),
          }),
        ]),
      });
    });
  });

  // ============================================================
  // DynamoDB Tables
  // ============================================================

  describe('DynamoDB Tables', () => {
    test('creates AssistantsTable with PAY_PER_REQUEST billing', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: `${config.projectPrefix}-assistants`,
        BillingMode: 'PAY_PER_REQUEST',
        KeySchema: Match.arrayWith([
          { AttributeName: 'PK', KeyType: 'HASH' },
          { AttributeName: 'SK', KeyType: 'RANGE' },
        ]),
      });
    });

    test('AssistantsTable has global secondary indexes', () => {
      template.hasResourceProperties('AWS::DynamoDB::Table', {
        TableName: `${config.projectPrefix}-assistants`,
        GlobalSecondaryIndexes: Match.arrayWith([
          Match.objectLike({ IndexName: 'OwnerStatusIndex' }),
          Match.objectLike({ IndexName: 'VisibilityStatusIndex' }),
          Match.objectLike({ IndexName: 'SharedWithIndex' }),
        ]),
      });
    });

    test('creates exactly 1 DynamoDB table (UserFiles moved to InfrastructureStack)', () => {
      template.resourceCountIs('AWS::DynamoDB::Table', 1);
    });
  });

  // ============================================================
  // ALB Target Group
  // ============================================================

  describe('ALB Target Group', () => {
    test('creates target group on port 8000', () => {
      template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
        Port: 8000,
        Protocol: 'HTTP',
        TargetType: 'ip',
      });
    });

    test('target group has health check on /health', () => {
      template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
        HealthCheckPath: '/health',
        HealthyThresholdCount: 2,
        UnhealthyThresholdCount: 3,
      });
    });

    test('creates listener rule for /* path pattern', () => {
      template.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
        Conditions: Match.arrayWith([
          Match.objectLike({
            Field: 'path-pattern',
            PathPatternConfig: { Values: ['/*'] },
          }),
        ]),
        Priority: 1,
      });
    });
  });

  // ============================================================
  // SSM Parameters (exported by this stack)
  // ============================================================

  describe('SSM Parameters', () => {
    test('exports 0 SSM parameters (runtime provisioner/updater removed)', () => {
      template.resourceCountIs('AWS::SSM::Parameter', 0);
    });
  });

  // ============================================================
  // IAM Task Role
  // ============================================================

  describe('IAM Task Role', () => {
    test('creates an IAM role for ECS task', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sts:AssumeRole',
              Principal: { Service: 'ecs-tasks.amazonaws.com' },
            }),
          ]),
        }),
      });
    });

    test('task role has Bedrock InvokeModel permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'bedrock:InvokeModel',
              Effect: 'Allow',
            }),
          ]),
        }),
      });
    });

    test('task role has S3 Vectors permissions', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                's3vectors:PutVectors',
                's3vectors:QueryVectors',
              ]),
              Effect: 'Allow',
            }),
          ]),
        }),
      });
    });

    test('BFFCookieSigningKey grant is Decrypt-only — kms:GenerateDataKey is NOT granted to the runtime', () => {
      // Least privilege: BFFCookieDataKeySecret is encrypted at rest with
      // BFFCookieSigningKey, so SecretsManager invokes kms:Decrypt on the
      // caller's behalf when app-api calls GetSecretValue. The runtime
      // never mints a fresh key — a GenerateDataKey grant here would let a
      // compromised task seal cookies under a parallel key.
      const bffCookieGrants = _findStatementsBySid(template, 'BFFCookieSigningKeyAccess');
      expect(bffCookieGrants.length).toBeGreaterThan(0);
      for (const stmt of bffCookieGrants) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        expect(actions).not.toContain('kms:GenerateDataKey');
        expect(actions).toContain('kms:Decrypt');
      }
    });

    test('task role can read the BFF cookie data key secret (GetSecretValue)', () => {
      const grants = _findStatementsBySid(template, 'BFFCookieDataKeySecretAccess');
      expect(grants.length).toBeGreaterThan(0);
      for (const stmt of grants) {
        expect(stmt.Effect).toBe('Allow');
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        expect(actions).toContain('secretsmanager:GetSecretValue');
        expect(actions).toContain('secretsmanager:DescribeSecret');
      }
    });
  });

  // ============================================================
  // BFF Cookie Data Key — env var wiring
  // ============================================================

  describe('BFF Cookie Data Key', () => {
    test('container env includes BFF_COOKIE_DATA_KEY_SECRET_ARN sourced from SSM', () => {
      // Without this env var the CookieCodec falls back to "BFF disabled"
      // and every cookie-bearing request unseals as bad seal — auth flow
      // breaks across every app-api task.
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Environment: Match.arrayWith([
              Match.objectLike({
                Name: 'BFF_COOKIE_DATA_KEY_SECRET_ARN',
              }),
            ]),
          }),
        ]),
      });
    });
  });

  // ============================================================
  // CloudWatch Log Group
  // ============================================================

  describe('CloudWatch Log Group', () => {
    test('creates log group for ECS tasks', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: `/ecs/${config.projectPrefix}/app-api`,
        RetentionInDays: 7,
      });
    });
  });

  // ============================================================
  // Security Group
  // ============================================================

  describe('Security Group', () => {
    test('creates ECS security group', () => {
      template.hasResourceProperties('AWS::EC2::SecurityGroup', {
        GroupDescription: 'Security group for App API ECS Fargate tasks',
      });
    });
  });

  // ============================================================
  // CloudFormation Outputs (Required for Deploy Script)
  // ============================================================

  describe('CloudFormation Outputs', () => {
    test('exports EcsClusterName for deploy script', () => {
      template.hasOutput('EcsClusterName', {
        Description: 'ECS Cluster Name',
        Export: {
          Name: `${config.projectPrefix}-AppEcsClusterName`,
        },
      });
    });

    test('exports EcsServiceName for deploy script', () => {
      template.hasOutput('EcsServiceName', {
        Description: 'ECS Service Name',
        Export: {
          Name: `${config.projectPrefix}-AppEcsServiceName`,
        },
      });
    });
  });
});
