/**
 * Integration test — single-stack architecture (post Phase 7 of the
 * platform-as-bootstrap refactor). Verifies that PlatformStack
 * called and that every resource the application needs is present
 * in exactly one stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('Single-stack integration', () => {
  let template: Template;

  beforeAll(() => {
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

    const platform = new PlatformStack(app, 'Platform', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    platform.wireCompute();

    template = Template.fromStack(platform);
  });

  describe('Stack contents', () => {
    it('produces a substantial template (the unified stack has ~150-200 resources)', () => {
      const json = template.toJSON();
      const count = Object.keys(json.Resources || {}).length;
      expect(count).toBeGreaterThan(80);
    });

    it('emits CFN outputs (auto-generated for cross-resource refs and explicit CfnOutputs)', () => {
      const outputs = template.findOutputs('*');
      expect(Object.keys(outputs).length).toBeGreaterThan(0);
    });

    it('uses zero Fn::ImportValue (no cross-stack refs in a single-stack architecture)', () => {
      const json = JSON.stringify(template.toJSON());
      // Allow for the rare CDK-internal use; just assert it isn't
      // peppered with project-scoped imports.
      const projectImports = (json.match(/Fn::ImportValue/g) || []).length;
      expect(projectImports).toBe(0);
    });
  });

  describe('Compute resources', () => {
    it('creates the App API Fargate service', () => {
      template.resourceCountIs('AWS::ECS::Service', 1);
    });

    it('creates the AgentCore Runtime', () => {
      template.resourceCountIs('AWS::BedrockAgentCore::Runtime', 1);
    });

    it('creates the AgentCore Memory + CI + Browser + Gateway', () => {
      template.resourceCountIs('AWS::BedrockAgentCore::Memory', 1);
      template.resourceCountIs('AWS::BedrockAgentCore::CodeInterpreterCustom', 1);
      template.resourceCountIs('AWS::BedrockAgentCore::BrowserCustom', 1);
      template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
    });
  });

  describe('Data + edge resources', () => {
    it('owns all DynamoDB tables', () => {
      const tables = Object.keys(template.findResources('AWS::DynamoDB::Table')).length;
      expect(tables).toBeGreaterThanOrEqual(20);
    });

    it('owns multiple S3 buckets', () => {
      const buckets = Object.keys(template.findResources('AWS::S3::Bucket')).length;
      expect(buckets).toBeGreaterThanOrEqual(5);
    });

    it('owns SPA + MCP sandbox + artifacts CloudFront distributions', () => {
      template.resourceCountIs('AWS::CloudFront::Distribution', 3);
    });

    it('owns Cognito user pool', () => {
      template.resourceCountIs('AWS::Cognito::UserPool', 1);
    });

    it('owns the VPC + ALB', () => {
      template.resourceCountIs('AWS::EC2::VPC', 1);
      template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    });
  });

  describe('Lambdas (artifact-render + rag-ingestion + CFN custom resources)', () => {
    it('has the two real Lambdas plus CFN custom-resource handlers', () => {
      const fns = Object.keys(template.findResources('AWS::Lambda::Function')).length;
      // Two real Lambdas (artifact-render + rag-ingestion). CDK
      // adds custom-resource handlers for things like S3 bucket
      // notification setup; the count is at least 2 but typically
      // a few more.
      expect(fns).toBeGreaterThanOrEqual(2);
    });

    it('publishes the auto-generated function names to SSM', () => {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/artifacts/render-function-name',
      });
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: '/test-project/rag/ingestion-function-name',
      });
    });
  });

  describe('Stack naming', () => {
    it('SSM parameter names include the project prefix', () => {
      const params = template.findResources('AWS::SSM::Parameter');
      const firstParam = Object.values(params)[0] as any;
      expect(firstParam.Properties.Name).toContain('test-project');
    });
  });
});

describe('Config validation', () => {
  it('loadConfig requires CDK_PROJECT_PREFIX', () => {
    const app = new cdk.App();
    expect(() => {
      const { loadConfig } = require('../lib/config');
      loadConfig(app);
    }).toThrow(/CDK_PROJECT_PREFIX/);
  });
});

describe('Restore tool exists', () => {
  const RESTORE = require('path').resolve(__dirname, '..', '..', 'scripts', 'restore-data');

  it('restore.py exists', () => {
    expect(require('fs').existsSync(require('path').join(RESTORE, 'restore.py'))).toBe(true);
  });

  it('pyproject.toml exists', () => {
    expect(require('fs').existsSync(require('path').join(RESTORE, 'pyproject.toml'))).toBe(true);
  });

  it('README.md exists', () => {
    expect(require('fs').existsSync(require('path').join(RESTORE, 'README.md'))).toBe(true);
  });

  it('restore.py has main() function', () => {
    const content = require('fs').readFileSync(require('path').join(RESTORE, 'restore.py'), 'utf-8');
    expect(content).toContain('def main()');
  });

  it('restore.py has --dry-run flag', () => {
    const content = require('fs').readFileSync(require('path').join(RESTORE, 'restore.py'), 'utf-8');
    expect(content).toContain('--dry-run');
  });

  it('restore.py is idempotent (catches DuplicateProviderException)', () => {
    const content = require('fs').readFileSync(require('path').join(RESTORE, 'restore.py'), 'utf-8');
    expect(content).toContain('DuplicateProviderException');
  });

  it('restore.py handles UsernameExistsException', () => {
    const content = require('fs').readFileSync(require('path').join(RESTORE, 'restore.py'), 'utf-8');
    expect(content).toContain('UsernameExistsException');
  });
});
