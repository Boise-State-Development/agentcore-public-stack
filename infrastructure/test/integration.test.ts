/**
 * Integration test — single-stack architecture (post Phase 7 of the
 * platform-as-bootstrap refactor). Verifies that PlatformStack
 * called and that every resource the application needs is present
 * in exactly one stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
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

    it('grants the AgentCore runtime role bedrock:CountTokens for context attribution', () => {
      // CountTokens is the foundation of per-turn context attribution —
      // Strands' native token counting decomposes the otherwise-aggregate
      // inputTokens into system / tools / messages partitions via this API.
      // The action lives in the BedrockModelInvocation statement alongside
      // the invoke actions and reuses its foundation-model resource scope.
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Sid: 'BedrockModelInvocation',
              Action: [
                'bedrock:InvokeModel',
                'bedrock:InvokeModelWithResponseStream',
                'bedrock:CountTokens',
              ],
            }),
          ]),
        },
      });
    });

    it('grants bedrock-mantle:CallWithBearerToken for Bedrock Mantle (runtime + app-api)', () => {
      // Bedrock Mantle (the OpenAI-compatible Bedrock surface) has its own
      // IAM service namespace — `bedrock-mantle:*`, NOT `bedrock:*`. It
      // authenticates with a presigned bearer token; the service authorizes
      // the signer against bedrock-mantle:CallWithBearerToken. The runtime
      // role needs it for mantle-provider inference, the app-api task role
      // for the GET /admin/mantle/models browse endpoint. Both statements
      // carry the same Sid, so assert the shape appears at least twice.
      const statementsOf = (resources: Record<string, any>) =>
        Object.values(resources).flatMap((res: any) => [
          ...(res.Properties?.PolicyDocument?.Statement ?? []),
          ...(res.Properties?.Policies ?? []).flatMap(
            (p: any) => p.PolicyDocument?.Statement ?? [],
          ),
        ]);
      const allStatements = [
        ...statementsOf(template.findResources('AWS::IAM::Policy')),
        ...statementsOf(template.findResources('AWS::IAM::ManagedPolicy')),
        ...statementsOf(template.findResources('AWS::IAM::Role')),
      ];
      const bearerStatements = allStatements.filter(
        (stmt: any) => stmt.Sid === 'BedrockMantleCallWithBearerToken',
      );
      expect(bearerStatements.length).toBeGreaterThanOrEqual(2);
      for (const stmt of bearerStatements) {
        expect(stmt.Action).toBe('bedrock-mantle:CallWithBearerToken');
        expect(stmt.Resource).toBe('*');
      }
      // The runtime role must also be able to create inferences.
      const inferenceStatement = allStatements.find(
        (stmt: any) => stmt.Sid === 'BedrockMantleInference',
      );
      expect(inferenceStatement).toBeDefined();
      expect(inferenceStatement.Action).toContain('bedrock-mantle:CreateInference');
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
