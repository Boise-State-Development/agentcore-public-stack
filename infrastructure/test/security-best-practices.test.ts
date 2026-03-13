/**
 * Cross-cutting security and best-practice tests.
 *
 * Synthesizes ALL stacks once and verifies that security-related
 * CloudFormation patterns (encryption, public access, IAM, tagging,
 * removal policies) are correct across every stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfrastructureStack } from '../lib/infrastructure-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { AppApiStack } from '../lib/app-api-stack';
import { InferenceApiStack } from '../lib/inference-api-stack';
import { GatewayStack } from '../lib/gateway-stack';
import { RagIngestionStack } from '../lib/rag-ingestion-stack';
import { createMockConfig, mockSsmContext, createMockApp, mockEnv } from './helpers/mock-config';

/* ------------------------------------------------------------------ */
/*  Non-production stacks (retainDataOnDelete: false)                 */
/* ------------------------------------------------------------------ */

let templates: Record<string, Template>;

beforeAll(() => {
  const config = createMockConfig(); // retainDataOnDelete: false
  const app = createMockApp(config);
  const env = mockEnv(config);

  const infraStack = new InfrastructureStack(app, 'Infra', { config, env });
  const ragStack = new RagIngestionStack(app, 'Rag', { config, env });
  const gatewayStack = new GatewayStack(app, 'Gateway', { config, env });
  const inferenceStack = new InferenceApiStack(app, 'Inference', { config, env });
  const appApiStack = new AppApiStack(app, 'AppApi', { config, env });
  const frontendStack = new FrontendStack(app, 'Frontend', { config, env });

  templates = {
    infrastructure: Template.fromStack(infraStack),
    ragIngestion: Template.fromStack(ragStack),
    gateway: Template.fromStack(gatewayStack),
    inferenceApi: Template.fromStack(inferenceStack),
    appApi: Template.fromStack(appApiStack),
    frontend: Template.fromStack(frontendStack),
  };
});

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

/** Collect resources of a given type across all templates. */
function findResourcesAcrossStacks(
  resourceType: string,
): { stackName: string; logicalId: string; resource: Record<string, unknown> }[] {
  const results: { stackName: string; logicalId: string; resource: Record<string, unknown> }[] = [];
  for (const [stackName, template] of Object.entries(templates)) {
    const resources = template.findResources(resourceType);
    for (const [logicalId, resource] of Object.entries(resources)) {
      results.push({ stackName, logicalId, resource: resource as Record<string, unknown> });
    }
  }
  return results;
}

/* ================================================================== */
/*  1. All S3 buckets have encryption enabled                         */
/* ================================================================== */

describe('S3 bucket encryption', () => {
  test('every S3 bucket has BucketEncryption configured', () => {
    const buckets = findResourcesAcrossStacks('AWS::S3::Bucket');
    expect(buckets.length).toBeGreaterThan(0);

    for (const { stackName, logicalId, resource } of buckets) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      expect(props).toBeDefined();
      expect(props!['BucketEncryption']).toBeDefined();
    }
  });
});

/* ================================================================== */
/*  2. All S3 buckets block public access                             */
/* ================================================================== */

describe('S3 public access block', () => {
  test('every S3 bucket blocks public access on all four flags', () => {
    const buckets = findResourcesAcrossStacks('AWS::S3::Bucket');
    expect(buckets.length).toBeGreaterThan(0);

    for (const { stackName, logicalId, resource } of buckets) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      const publicBlock = props?.['PublicAccessBlockConfiguration'] as
        | Record<string, unknown>
        | undefined;

      expect(publicBlock).toBeDefined();
      expect(publicBlock!['BlockPublicAcls']).toBe(true);
      expect(publicBlock!['BlockPublicPolicy']).toBe(true);
      expect(publicBlock!['IgnorePublicAcls']).toBe(true);
      expect(publicBlock!['RestrictPublicBuckets']).toBe(true);
    }
  });
});

/* ================================================================== */
/*  3. All DynamoDB tables have encryption                            */
/* ================================================================== */

describe('DynamoDB encryption', () => {
  test('every DynamoDB table has SSE enabled or uses default encryption', () => {
    const tables = findResourcesAcrossStacks('AWS::DynamoDB::Table');
    // Some stacks may not define DynamoDB tables — that is fine.
    // When tables exist, they must be encrypted.
    for (const { stackName, logicalId, resource } of tables) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      const sse = props?.['SSESpecification'] as Record<string, unknown> | undefined;

      // Either SSESpecification.SSEEnabled is true, or the property is
      // absent (AWS default encryption applies).
      if (sse) {
        expect(sse['SSEEnabled']).toBe(true);
      }
      // Absence of SSESpecification is acceptable — AWS encrypts by default.
    }
  });
});

/* ================================================================== */
/*  4. No wildcard IAM policies (Action: "*" AND Resource: "*")       */
/* ================================================================== */

describe('IAM wildcard policies', () => {
  /** Recursively check whether a value resolves to the string "*". */
  function isWildcard(value: unknown): boolean {
    if (value === '*') return true;
    if (Array.isArray(value) && value.length === 1 && value[0] === '*') return true;
    return false;
  }

  function extractStatements(policyDoc: unknown): Record<string, unknown>[] {
    if (!policyDoc || typeof policyDoc !== 'object') return [];
    const doc = policyDoc as Record<string, unknown>;
    const stmts = doc['Statement'];
    if (Array.isArray(stmts)) return stmts as Record<string, unknown>[];
    return [];
  }

  test('no IAM policy has both Action: "*" and Resource: "*"', () => {
    const policyResources = findResourcesAcrossStacks('AWS::IAM::Policy');
    const roleResources = findResourcesAcrossStacks('AWS::IAM::Role');

    const violations: string[] = [];

    // Check inline policies on IAM::Policy resources
    for (const { stackName, logicalId, resource } of policyResources) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      const statements = extractStatements(props?.['PolicyDocument']);
      for (const stmt of statements) {
        if (isWildcard(stmt['Action']) && isWildcard(stmt['Resource'])) {
          violations.push(`${stackName}/${logicalId}: Action=* Resource=*`);
        }
      }
    }

    // Check inline policies embedded in IAM::Role resources
    for (const { stackName, logicalId, resource } of roleResources) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      const inlinePolicies = props?.['Policies'] as Record<string, unknown>[] | undefined;
      if (!Array.isArray(inlinePolicies)) continue;
      for (const policy of inlinePolicies) {
        const statements = extractStatements(policy['PolicyDocument']);
        for (const stmt of statements) {
          if (isWildcard(stmt['Action']) && isWildcard(stmt['Resource'])) {
            violations.push(`${stackName}/${logicalId}: Action=* Resource=*`);
          }
        }
      }
    }

    expect(violations).toEqual([]);
  });
});

/* ================================================================== */
/*  5. All stacks apply standard tags (Project, Version)              */
/* ================================================================== */

describe('Standard tags', () => {
  test('every stack with taggable resources has Project and Version tags', () => {
    for (const [stackName, template] of Object.entries(templates)) {
      const allResources = template.toJSON()['Resources'] as Record<string, Record<string, unknown>>;

      // Find at least one taggable resource that carries the standard tags.
      const taggedResources = Object.entries(allResources).filter(([, res]) => {
        const props = res['Properties'] as Record<string, unknown> | undefined;
        return Array.isArray(props?.['Tags']);
      });

      // Some stacks might only have non-taggable resources — skip those.
      if (taggedResources.length === 0) continue;

      let hasProject = false;
      let hasVersion = false;

      for (const [, res] of taggedResources) {
        const tags = (res['Properties'] as Record<string, unknown>)['Tags'] as {
          Key: string;
          Value: string;
        }[];
        if (tags.some((t) => t.Key === 'Project')) hasProject = true;
        if (tags.some((t) => t.Key === 'Version')) hasVersion = true;
        if (hasProject && hasVersion) break;
      }

      expect({ stack: stackName, hasProject }).toEqual({ stack: stackName, hasProject: true });
      expect({ stack: stackName, hasVersion }).toEqual({ stack: stackName, hasVersion: true });
    }
  });
});

/* ================================================================== */
/*  6. DynamoDB tables use PAY_PER_REQUEST billing                    */
/* ================================================================== */

describe('DynamoDB billing mode', () => {
  test('every DynamoDB table uses PAY_PER_REQUEST', () => {
    const tables = findResourcesAcrossStacks('AWS::DynamoDB::Table');

    for (const { stackName, logicalId, resource } of tables) {
      const props = resource['Properties'] as Record<string, unknown> | undefined;
      expect(props?.['BillingMode']).toBe('PAY_PER_REQUEST');
    }
  });
});

/* ================================================================== */
/*  7. Non-production stacks use DESTROY removal policy               */
/* ================================================================== */

describe('Non-production removal policies', () => {
  test('config-driven S3 buckets have DeletionPolicy: Delete when retainDataOnDelete is false', () => {
    const buckets = findResourcesAcrossStacks('AWS::S3::Bucket');
    expect(buckets.length).toBeGreaterThan(0);

    // Some document-storage buckets are intentionally always-RETAIN for data
    // safety regardless of the retainDataOnDelete flag. Verify that the
    // config-driven mechanism works: at least one bucket should use Delete.
    const deleteBuckets = buckets.filter((b) => b.resource['DeletionPolicy'] === 'Delete');
    const retainBuckets = buckets.filter((b) => b.resource['DeletionPolicy'] === 'Retain');

    expect(deleteBuckets.length).toBeGreaterThan(0);

    // Every bucket should be either Delete (config-driven) or Retain
    // (hardcoded for data-safety). No other policy should appear.
    for (const { stackName, logicalId, resource } of buckets) {
      expect(['Delete', 'Retain']).toContain(resource['DeletionPolicy']);
    }
  });

  test('DynamoDB tables have DeletionPolicy: Delete when retainDataOnDelete is false', () => {
    const tables = findResourcesAcrossStacks('AWS::DynamoDB::Table');

    for (const { stackName, logicalId, resource } of tables) {
      expect(resource['DeletionPolicy']).toBe('Delete');
    }
  });
});

/* ================================================================== */
/*  8. Production stacks use RETAIN removal policy                    */
/* ================================================================== */

describe('Production removal policies', () => {
  let prodTemplates: Record<string, Template>;

  beforeAll(() => {
    const prodConfig = createMockConfig({ retainDataOnDelete: true });
    const prodApp = createMockApp(prodConfig);
    const prodEnv = mockEnv(prodConfig);

    const infraStack = new InfrastructureStack(prodApp, 'ProdInfra', { config: prodConfig, env: prodEnv });
    const ragStack = new RagIngestionStack(prodApp, 'ProdRag', { config: prodConfig, env: prodEnv });
    const gatewayStack = new GatewayStack(prodApp, 'ProdGateway', { config: prodConfig, env: prodEnv });
    const inferenceStack = new InferenceApiStack(prodApp, 'ProdInference', { config: prodConfig, env: prodEnv });
    const appApiStack = new AppApiStack(prodApp, 'ProdAppApi', { config: prodConfig, env: prodEnv });
    const frontendStack = new FrontendStack(prodApp, 'ProdFrontend', { config: prodConfig, env: prodEnv });

    prodTemplates = {
      infrastructure: Template.fromStack(infraStack),
      ragIngestion: Template.fromStack(ragStack),
      gateway: Template.fromStack(gatewayStack),
      inferenceApi: Template.fromStack(inferenceStack),
      appApi: Template.fromStack(appApiStack),
      frontend: Template.fromStack(frontendStack),
    };
  });

  function findProdResources(
    resourceType: string,
  ): { stackName: string; logicalId: string; resource: Record<string, unknown> }[] {
    const results: { stackName: string; logicalId: string; resource: Record<string, unknown> }[] = [];
    for (const [stackName, template] of Object.entries(prodTemplates)) {
      const resources = template.findResources(resourceType);
      for (const [logicalId, resource] of Object.entries(resources)) {
        results.push({ stackName, logicalId, resource: resource as Record<string, unknown> });
      }
    }
    return results;
  }

  test('S3 buckets have DeletionPolicy: Retain when retainDataOnDelete is true', () => {
    const buckets = findProdResources('AWS::S3::Bucket');
    expect(buckets.length).toBeGreaterThan(0);

    for (const { stackName, logicalId, resource } of buckets) {
      expect(resource['DeletionPolicy']).toBe('Retain');
    }
  });

  test('DynamoDB tables have DeletionPolicy: Retain when retainDataOnDelete is true', () => {
    const tables = findProdResources('AWS::DynamoDB::Table');

    for (const { stackName, logicalId, resource } of tables) {
      expect(resource['DeletionPolicy']).toBe('Retain');
    }
  });
});
