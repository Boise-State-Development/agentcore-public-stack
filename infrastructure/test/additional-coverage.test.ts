/**
 * Additional coverage — config defaults, SSM parameter naming, and
 * construct property types.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';
import { AppConfig } from '../lib/config';

import { NetworkConstruct } from '../lib/constructs/network/network-construct';
import { AuthTablesConstruct } from '../lib/constructs/data/auth-tables-construct';
import { QuotaTablesConstruct } from '../lib/constructs/data/quota-tables-construct';
import { CostTrackingTablesConstruct } from '../lib/constructs/data/cost-tracking-tables-construct';
import { FileUploadConstruct } from '../lib/constructs/data/file-upload-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { FineTuningDataConstruct } from '../lib/constructs/fine-tuning/fine-tuning-data-construct';
import { CognitoConstruct } from '../lib/constructs/identity/cognito-construct';
import { OAuthTablesConstruct } from '../lib/constructs/identity/oauth-tables-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

describe('SSM parameter naming convention', () => {
  const config = createMockConfig();

  it('all network params start with /{prefix}/network/', () => {
    const stack = testStack();
    new NetworkConstruct(stack, 'Net', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      expect((resource as any).Properties.Name).toMatch(/^\/test-project\/network\//);
    }
  });

  it('all auth table params start with /{prefix}/auth/ or /{prefix}/users/ or /{prefix}/rbac/', () => {
    const stack = testStack();
    new AuthTablesConstruct(stack, 'AT', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      const name = (resource as any).Properties.Name as string;
      expect(name).toMatch(/^\/test-project\/(auth|users|rbac)\//);
    }
  });

  it('all quota params start with /{prefix}/quota/', () => {
    const stack = testStack();
    new QuotaTablesConstruct(stack, 'QT', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      expect((resource as any).Properties.Name).toMatch(/^\/test-project\/quota\//);
    }
  });

  it('all cost-tracking params start with /{prefix}/cost-tracking/ or /{prefix}/admin/', () => {
    const stack = testStack();
    new CostTrackingTablesConstruct(stack, 'CT', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      const name = (resource as any).Properties.Name as string;
      expect(name).toMatch(/^\/test-project\/(cost-tracking|admin)\//);
    }
  });

  it('all file-upload params start with /{prefix}/user-file-uploads/', () => {
    const stack = testStack();
    new FileUploadConstruct(stack, 'FU', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      expect((resource as any).Properties.Name).toMatch(/^\/test-project\/user-file-uploads\//);
    }
  });

  it('all RAG params start with /{prefix}/rag/', () => {
    const stack = testStack();
    new RagDataConstruct(stack, 'Rag', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      expect((resource as any).Properties.Name).toMatch(/^\/test-project\/rag\//);
    }
  });

  it('all fine-tuning params start with /{prefix}/fine-tuning/', () => {
    const stack = testStack();
    new FineTuningDataConstruct(stack, 'FT', { config });
    const t = Template.fromStack(stack);
    const params = t.findResources('AWS::SSM::Parameter');
    for (const [, resource] of Object.entries(params)) {
      expect((resource as any).Properties.Name).toMatch(/^\/test-project\/fine-tuning\//);
    }
  });
});

describe('Construct property types', () => {
  const config = createMockConfig();

  it('NetworkConstruct.vpc is an IVpc', () => {
    const stack = testStack();
    const net = new NetworkConstruct(stack, 'Net', { config });
    expect(net.vpc.vpcId).toBeDefined();
    expect(net.vpc.privateSubnets.length).toBeGreaterThan(0);
    expect(net.vpc.publicSubnets.length).toBeGreaterThan(0);
  });

  it('AuthTablesConstruct exposes all 5 tables', () => {
    const stack = testStack();
    const at = new AuthTablesConstruct(stack, 'AT', { config });
    expect(at.oidcStateTable.tableName).toBeDefined();
    expect(at.bffSessionsTable.tableName).toBeDefined();
    expect(at.usersTable.tableName).toBeDefined();
    expect(at.appRolesTable.tableName).toBeDefined();
    expect(at.apiKeysTable.tableName).toBeDefined();
  });

  it('QuotaTablesConstruct exposes both tables', () => {
    const stack = testStack();
    const qt = new QuotaTablesConstruct(stack, 'QT', { config });
    expect(qt.userQuotasTable.tableName).toBeDefined();
    expect(qt.quotaEventsTable.tableName).toBeDefined();
  });

  it('CostTrackingTablesConstruct exposes all 4 tables', () => {
    const stack = testStack();
    const ct = new CostTrackingTablesConstruct(stack, 'CT', { config });
    expect(ct.sessionsMetadataTable.tableName).toBeDefined();
    expect(ct.userCostSummaryTable.tableName).toBeDefined();
    expect(ct.systemCostRollupTable.tableName).toBeDefined();
    expect(ct.managedModelsTable.tableName).toBeDefined();
  });

  it('FileUploadConstruct exposes bucket and table', () => {
    const stack = testStack();
    const fu = new FileUploadConstruct(stack, 'FU', { config });
    expect(fu.bucket.bucketName).toBeDefined();
    expect(fu.table.tableName).toBeDefined();
  });

  it('RagDataConstruct exposes all resources', () => {
    const stack = testStack();
    const rag = new RagDataConstruct(stack, 'Rag', { config });
    expect(rag.documentsBucket.bucketName).toBeDefined();
    expect(rag.assistantsTable.tableName).toBeDefined();
    expect(rag.vectorBucketName).toBeDefined();
    expect(rag.vectorIndexName).toBeDefined();
  });

  it('CognitoConstruct exposes user pool and client', () => {
    const stack = testStack();
    const cog = new CognitoConstruct(stack, 'Cog', { config });
    expect(cog.userPool.userPoolId).toBeDefined();
    expect(cog.bffAppClient.userPoolClientId).toBeDefined();
    expect(cog.cognitoDomain).toBeDefined();
  });

  it('OAuthTablesConstruct exposes tables + key + secret', () => {
    const stack = testStack();
    const oauth = new OAuthTablesConstruct(stack, 'OAuth', { config });
    expect(oauth.providersTable.tableName).toBeDefined();
    expect(oauth.userTokensTable.tableName).toBeDefined();
    expect(oauth.tokenEncryptionKey.keyArn).toBeDefined();
    expect(oauth.clientSecretsSecret.secretArn).toBeDefined();
  });
});

describe('Config defaults', () => {
  it('createMockConfig produces a valid AppConfig', () => {
    const config = createMockConfig();
    expect(config.projectPrefix).toBe('test-project');
    expect(config.awsRegion).toBe('us-east-1');
    expect(config.vpcCidr).toBe('10.0.0.0/16');
  });

  it('createMockConfig allows overrides', () => {
    const config = createMockConfig({ projectPrefix: 'custom' });
    expect(config.projectPrefix).toBe('custom');
  });

  it('createMockConfig has all required fields', () => {
    const config = createMockConfig();
    const requiredKeys: (keyof AppConfig)[] = [
      'projectPrefix', 'awsAccount', 'awsRegion', 'vpcCidr',
      'frontend', 'appApi', 'inferenceApi',
      'ragIngestion', 'fineTuning', 'artifacts',
      'mcpSandbox', 'cognito', 'tags',
    ];
    for (const key of requiredKeys) {
      expect(config[key]).toBeDefined();
    }
  });

  it('default artifacts.retentionDays is 90', () => {
    const config = createMockConfig();
    expect(config.artifacts.retentionDays).toBe(90);
  });

  it('default appApi.cpu is 256', () => {
    const config = createMockConfig();
    expect(config.appApi.cpu).toBe(256);
  });

  it('default ragIngestion.vectorDimension is 1024', () => {
    const config = createMockConfig();
    expect(config.ragIngestion.vectorDimension).toBe(1024);
  });
});
