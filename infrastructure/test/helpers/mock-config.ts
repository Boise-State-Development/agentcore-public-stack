/**
 * Shared mock configuration factory for CDK stack tests.
 *
 * Provides a complete, valid AppConfig and helpers to mock SSM
 * valueFromLookup context entries so stacks can be synthesized
 * without hitting AWS.
 */
import * as cdk from 'aws-cdk-lib';
import { AppConfig } from '../../lib/config';

/** Default mock account and region used across all tests. */
export const MOCK_ACCOUNT = '123456789012';
export const MOCK_REGION = 'us-east-1';
export const MOCK_PREFIX = 'test-project';

/**
 * Build a complete AppConfig suitable for test synthesis.
 * Pass partial overrides to customise individual fields.
 */
export function createMockConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  const base: AppConfig = {
    projectPrefix: MOCK_PREFIX,
    awsAccount: MOCK_ACCOUNT,
    awsRegion: MOCK_REGION,
    production: false,
    retainDataOnDelete: false,
    vpcCidr: '10.0.0.0/16',
    corsOrigins: 'http://localhost:4200',
    appVersion: '1.0.0-test',
    frontend: {
      enabled: true,
      cloudFrontPriceClass: 'PriceClass_100',
    },
    appApi: {
      enabled: true,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 2,
      imageTag: 'latest',
    },
    inferenceApi: {
      enabled: true,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 2,
      imageTag: 'latest',
      logLevel: 'INFO',
      corsOrigins: 'http://localhost:4200',
    },
    gateway: {
      enabled: true,
      apiType: 'REST',
      throttleRateLimit: 100,
      throttleBurstLimit: 50,
      enableWaf: false,
    },
    assistants: {
      enabled: true,
      corsOrigins: 'http://localhost:4200',
    },
    fileUpload: {
      enabled: true,
      maxFileSizeBytes: 10485760,
      maxFilesPerMessage: 5,
      userQuotaBytes: 104857600,
      retentionDays: 30,
    },
    ragIngestion: {
      enabled: true,
      corsOrigins: 'http://localhost:4200',
      lambdaMemorySize: 3008,
      lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2',
      vectorDimension: 1024,
      vectorDistanceMetric: 'cosine',
    },
    fineTuning: {
      enabled: false,
      defaultQuotaHours: 0,
    },
    tags: { ManagedBy: 'CDK', Environment: 'test' },
  };

  return { ...base, ...overrides };
}

/**
 * Map of every SSM parameter that each stack reads via valueFromLookup
 * or valueForStringParameter, keyed by a human-friendly stack label.
 *
 * Keep this in sync with the actual stack source.  The stack-dependencies
 * test will verify correctness by scanning source files.
 */
const SSM_READS_BY_STACK: Record<string, string[]> = {
  InfrastructureStack: [],
  FrontendStack: [
    'network/alb-url',
  ],
  RagIngestionStack: [
    'network/vpc-id',
    'network/vpc-cidr',
    'network/private-subnet-ids',
    'network/availability-zones',
    'rag-ingestion/image-tag',
  ],
  GatewayStack: [],
  SageMakerFineTuningStack: [
    'network/vpc-id',
    'network/vpc-cidr',
    'network/private-subnet-ids',
    'network/availability-zones',
  ],
  InferenceApiStack: [
    'inference-api/image-tag',
    'oauth/client-secrets-arn',
    'users/users-table-arn',
    'rbac/app-roles-table-arn',
    'oauth/providers-table-arn',
    'oauth/user-tokens-table-arn',
    'oauth/token-encryption-key-arn',
    'auth/api-keys-table-arn',
    'rag/assistants-table-arn',
    'rag/vector-bucket-name',
    'quota/user-quotas-table-arn',
    'quota/quota-events-table-arn',
    'cost-tracking/sessions-metadata-table-arn',
    'cost-tracking/user-cost-summary-table-arn',
    'cost-tracking/system-cost-rollup-table-arn',
    'admin/managed-models-table-arn',
    'auth/auth-providers-table-arn',
    'auth/auth-provider-secrets-arn',
    'user-file-uploads/table-arn',
    'user-file-uploads/bucket-arn',
  ],
  AppApiStack: [
    'network/vpc-id',
    'network/vpc-cidr',
    'network/private-subnet-ids',
    'network/availability-zones',
    'app-api/image-tag',
    'network/alb-security-group-id',
    'network/alb-arn',
    'network/alb-listener-arn',
    'network/ecs-cluster-name',
    'network/ecs-cluster-arn',
    'auth/oidc-state-table-name',
    'auth/oidc-state-table-arn',
    'users/users-table-name',
    'users/users-table-arn',
    'rbac/app-roles-table-name',
    'rbac/app-roles-table-arn',
    'auth/api-keys-table-name',
    'auth/api-keys-table-arn',
    'oauth/providers-table-name',
    'oauth/providers-table-arn',
    'oauth/user-tokens-table-name',
    'oauth/user-tokens-table-arn',
    'oauth/token-encryption-key-arn',
    'oauth/client-secrets-arn',
    'quota/user-quotas-table-name',
    'quota/user-quotas-table-arn',
    'quota/quota-events-table-name',
    'quota/quota-events-table-arn',
    'cost-tracking/sessions-metadata-table-name',
    'cost-tracking/sessions-metadata-table-arn',
    'cost-tracking/user-cost-summary-table-name',
    'cost-tracking/user-cost-summary-table-arn',
    'cost-tracking/system-cost-rollup-table-name',
    'cost-tracking/system-cost-rollup-table-arn',
    'admin/managed-models-table-name',
    'admin/managed-models-table-arn',
    'auth/auth-providers-table-name',
    'auth/auth-providers-table-arn',
    'auth/auth-providers-stream-arn',
    'auth/auth-provider-secrets-arn',
    'rag/documents-bucket-name',
    'rag/assistants-table-name',
    'rag/vector-bucket-name',
    'rag/vector-index-name',
    'inference-api/memory-id',
    'rag/documents-bucket-arn',
    'rag/assistants-table-arn',
    'inference-api/runtime-execution-role-arn',
    'inference-api/memory-arn',
    'fine-tuning/jobs-table-name',
    'fine-tuning/jobs-table-arn',
    'fine-tuning/access-table-name',
    'fine-tuning/access-table-arn',
    'fine-tuning/data-bucket-name',
    'fine-tuning/data-bucket-arn',
    'fine-tuning/sagemaker-execution-role-arn',
    'fine-tuning/sagemaker-security-group-id',
    'fine-tuning/private-subnet-ids',
    'user-file-uploads/bucket-name',
    'user-file-uploads/bucket-arn',
    'user-file-uploads/table-name',
    'user-file-uploads/table-arn',
  ],
};

/**
 * Mock SSM context entries for valueFromLookup calls.
 *
 * @param app   CDK App instance
 * @param config  AppConfig (uses projectPrefix, awsAccount, awsRegion)
 * @param stackNames  Which stacks' SSM reads to mock. Defaults to ALL stacks.
 */
export function mockSsmContext(
  app: cdk.App,
  config: AppConfig,
  stackNames?: string[],
): void {
  const stacks = stackNames ?? Object.keys(SSM_READS_BY_STACK);
  const seen = new Set<string>();

  for (const name of stacks) {
    const params = SSM_READS_BY_STACK[name];
    if (!params) {
      throw new Error(`Unknown stack name for SSM mocking: ${name}`);
    }
    for (const suffix of params) {
      if (seen.has(suffix)) continue;
      seen.add(suffix);

      const fullPath = `/${config.projectPrefix}/${suffix}`;
      const contextKey =
        `ssm:account=${config.awsAccount}:parameterName=${fullPath}:region=${config.awsRegion}`;

      // Provide realistic-looking dummy values based on the param suffix
      app.node.setContext(contextKey, getMockValueForParam(suffix));
    }
  }
}

/** Return a sensible dummy value given an SSM parameter path suffix. */
function getMockValueForParam(suffix: string): string {
  if (suffix.includes('vpc-id')) return 'vpc-12345';
  if (suffix.includes('vpc-cidr')) return '10.0.0.0/16';
  if (suffix.includes('subnet-ids')) return 'subnet-aaa,subnet-bbb';
  if (suffix.includes('availability-zones')) return 'us-east-1a,us-east-1b';
  if (suffix.includes('security-group-id')) return 'sg-12345';
  if (suffix.includes('alb-arn')) return 'arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/test/1234';
  if (suffix.includes('listener-arn')) return 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/test/1234/5678';
  if (suffix.includes('cluster-name')) return 'test-cluster';
  if (suffix.includes('cluster-arn')) return 'arn:aws:ecs:us-east-1:123456789012:cluster/test-cluster';
  if (suffix.includes('table-name')) return `mock-${suffix.replace(/\//g, '-')}-table`;
  if (suffix.includes('table-arn')) return `arn:aws:dynamodb:us-east-1:123456789012:table/mock-table`;
  if (suffix.includes('bucket-name')) return `mock-${suffix.replace(/\//g, '-')}-bucket`;
  if (suffix.includes('bucket-arn')) return `arn:aws:s3:::mock-bucket`;
  if (suffix.includes('secret') || suffix.includes('encryption-key')) return 'arn:aws:secretsmanager:us-east-1:123456789012:secret:mock-secret';
  if (suffix.includes('stream-arn')) return 'arn:aws:dynamodb:us-east-1:123456789012:table/mock/stream/2024';
  if (suffix.includes('role-arn')) return 'arn:aws:iam::123456789012:role/mock-role';
  if (suffix.includes('memory-id')) return 'mem-12345';
  if (suffix.includes('memory-arn')) return 'arn:aws:bedrock:us-east-1:123456789012:memory/mem-12345';
  if (suffix.includes('image-tag')) return 'latest';
  if (suffix.includes('index-name')) return 'mock-vector-index';
  if (suffix.includes('lambda') || suffix.includes('provisioner') || suffix.includes('updater')) return 'arn:aws:lambda:us-east-1:123456789012:function:mock-fn';
  if (suffix.includes('url')) return 'https://mock-api.example.com';
  if (suffix.includes('cors')) return 'http://localhost:4200';
  return 'mock-value';
}

/** Convenience: create a CDK App with SSM context pre-mocked for a given stack. */
export function createMockApp(config: AppConfig, stackNames?: string[]): cdk.App {
  const app = new cdk.App();
  mockSsmContext(app, config, stackNames);
  return app;
}

/** Standard CDK env object for test stacks. */
export function mockEnv(config: AppConfig): cdk.Environment {
  return { account: config.awsAccount, region: config.awsRegion };
}
