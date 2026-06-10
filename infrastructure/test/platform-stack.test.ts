/**
 * PlatformStack assertion tests.
 *
 * Verifies that PlatformStack synthesizes correctly and exposes all
 * required typed properties for BackendStack consumption.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('PlatformStack', () => {
  let stack: PlatformStack;
  let template: Template;

  beforeAll(() => {
    // Provide domain + certs so all constructs can synthesize.
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {},
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    // Wire the SPA distribution (requires ALB URL)
    template = Template.fromStack(stack);
  });

  describe('Network resources', () => {
    it('creates a VPC', () => {
      template.resourceCountIs('AWS::EC2::VPC', 1);
    });

    it('creates public and private subnets', () => {
      template.resourceCountIs('AWS::EC2::Subnet', 4); // 2 AZs × 2 types
    });

    it('creates a NAT gateway', () => {
      template.resourceCountIs('AWS::EC2::NatGateway', 1);
    });

    it('creates an internet gateway', () => {
      template.resourceCountIs('AWS::EC2::InternetGateway', 1);
    });

    it('creates an ALB', () => {
      template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    });

    it('creates an ALB listener', () => {
      template.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 2); // HTTPS + HTTP redirect
    });

    it('creates ALB security group', () => {
      template.resourceCountIs('AWS::EC2::SecurityGroup', 1);
    });

    it('creates an ECS cluster', () => {
      template.resourceCountIs('AWS::ECS::Cluster', 1);
    });
  });

  describe('Identity resources', () => {
    it('creates Cognito user pool', () => {
      template.resourceCountIs('AWS::Cognito::UserPool', 1);
    });

    it('creates Cognito user pool client', () => {
      template.resourceCountIs('AWS::Cognito::UserPoolClient', 1);
    });

    it('creates Cognito domain', () => {
      template.resourceCountIs('AWS::Cognito::UserPoolDomain', 1);
    });

    it('creates the platform workload identity', () => {
      template.resourceCountIs('AWS::BedrockAgentCore::WorkloadIdentity', 1);
    });

    it('creates Secrets Manager secrets', () => {
      // auth secret, voice ticket signing, BFF cookie data key,
      // OAuth client secrets, auth provider secrets, Cognito BFF client secret,
      // artifact render token (always-on now)
      template.resourceCountIs('AWS::SecretsManager::Secret', 7);
    });

    it('creates KMS keys', () => {
      // OAuth token encryption + BFF cookie signing
      template.resourceCountIs('AWS::KMS::Key', 2);
    });
  });

  describe('DynamoDB tables', () => {
    it('creates all shared tables', () => {
      // 24 tables. Was 23 — the system-prompts table was added for
      // admin-managed Conversation Modes (custom system prompt catalog).
      // Previously was 24 before the standalone "assistants" table
      // was decommissioned (the python app uses rag-assistants for
      // both assistant config and document metadata via
      // DYNAMODB_ASSISTANTS_TABLE_NAME).
      template.resourceCountIs('AWS::DynamoDB::Table', 24);
    });
  });

  describe('S3 buckets', () => {
    it('creates all data buckets', () => {
      // file-uploads, SPA static, mcp-sandbox, rag-documents, fine-tuning-data,
      // artifacts-content, skill-resources (admin-managed Skills reference files)
      template.resourceCountIs('AWS::S3::Bucket', 7);
    });
  });

  describe('CloudFront distributions', () => {
    it('creates SPA + mcp-sandbox + artifacts distributions', () => {
      // Phase 3 of the platform-as-bootstrap refactor moved the
      // artifacts distribution into PlatformStack alongside the
      // SPA + mcp-sandbox distributions. Three total now.
      template.resourceCountIs('AWS::CloudFront::Distribution', 3);
    });

    it('creates CloudFront functions', () => {
      // SPA: api-path-strip + spa-routing
      // MCP sandbox: csp-function
      template.resourceCountIs('AWS::CloudFront::Function', 3);
    });
  });

  describe('SSM parameters', () => {
    it('publishes only the SSM parameters deploy scripts, restore tooling, and e2e tests need', () => {
      // SSM publishes fall into a few buckets:
      //   - Deploy-time discovery (build, deploy-ecs-service,
      //     deploy-runtime-image, deploy-image-lambda, frontend deploy).
      //   - e2e test discovery.
      //   - Restore tooling (scripts/restore-data/restore.py): looks up
      //     every backed-up DynamoDB table, S3 bucket, Cognito user pool,
      //     and AgentCore Memory ID via SSM under /{prefix}/. These
      //     are kept in sync with TABLE_SSM_MAP / BUCKET_SSM_MAP /
      //     SSM_USER_POOL_ID / SSM_MEMORY_ID in restore.py.
      // Every other SSM publish was dead weight: the value was either
      // consumed only by sibling CDK constructs (now sourced via typed
      // PlatformComputeRefs) or never read by anyone.
      const params = template.findResources('AWS::SSM::Parameter');
      expect(Object.keys(params).length).toBeGreaterThanOrEqual(30);
      expect(Object.keys(params).length).toBeLessThanOrEqual(45);
    });
  });

  describe('Typed properties', () => {
    it('exposes vpc', () => {
      expect(stack.vpc).toBeDefined();
    });

    it('exposes alb', () => {
      expect(stack.alb).toBeDefined();
    });

    it('exposes albListener', () => {
      expect(stack.albListener).toBeDefined();
    });

    it('exposes ecsCluster', () => {
      expect(stack.ecsCluster).toBeDefined();
    });

    it('exposes authSecret', () => {
      expect(stack.authSecret).toBeDefined();
    });

    it('exposes userPool', () => {
      expect(stack.userPool).toBeDefined();
    });

    it('exposes fileUploadBucket', () => {
      expect(stack.fileUploadBucket).toBeDefined();
    });

    it('exposes ragDocumentsBucket', () => {
      expect(stack.ragDocumentsBucket).toBeDefined();
    });

    it('exposes artifactsContentBucket', () => {
      expect(stack.artifactsContentBucket).toBeDefined();
    });

    it('exposes fineTuningDataBucket', () => {
      expect(stack.fineTuningDataBucket).toBeDefined();
    });

    it('exposes artifactsTable', () => {
      expect(stack.artifactsTable).toBeDefined();
    });

    it('exposes fineTuningJobsTable', () => {
      expect(stack.fineTuningJobsTable).toBeDefined();
    });

    it('exposes mcpSandboxProxyOrigin', () => {
      expect(stack.mcpSandboxProxyOrigin).toBeDefined();
    });

    it('exposes spaDistribution after wiring', () => {
      expect(stack.spaDistribution).toBeDefined();
    });

    it('exposes artifactsFrameAncestors', () => {
      expect(stack.artifactsFrameAncestors).toContain('https://example.com');
    });
  });
});
