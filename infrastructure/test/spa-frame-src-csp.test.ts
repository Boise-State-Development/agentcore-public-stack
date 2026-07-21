/**
 * Regression cover for the SPA distribution's `frame-src` CSP.
 *
 * The SPA frames two cross-origin iframes on domained deploys: artifact
 * previews (`artifacts.{domain}`) and MCP App UIs via the sandbox proxy
 * (`mcp-sandbox.{domain}`). The sandbox side's `frame-ancestors` was
 * locked to the SPA origin from the start, but the SPA side's
 * `frame-src` originally listed only the artifacts origin — so deployed
 * environments blocked every MCP App iframe with a CSP violation while
 * localhost dev (which bypasses CloudFront's response headers) worked,
 * masking the gap through live verification.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const SHARED_CF_CERT = 'arn:aws:acm:us-east-1:123456789012:certificate/shared-wildcard';

/** Seed every context value loadConfig requires for a domained deploy. */
function seedRequiredContext(app: cdk.App): void {
  app.node.setContext('projectPrefix', 'test-project');
  app.node.setContext('awsRegion', MOCK_REGION);
  app.node.setContext('awsAccount', MOCK_ACCOUNT);
  app.node.setContext('vpcCidr', '10.0.0.0/16');
  app.node.setContext('production', false);
  app.node.setContext('retainDataOnDelete', false);
  app.node.setContext('domainName', 'example.com');
  app.node.setContext('infrastructureHostedZoneDomain', 'example.com');
  app.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
  app.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 2 });
  app.node.setContext('inferenceApi', {});
  app.node.setContext('fineTuning', {});
  app.node.setContext('artifacts', { retentionDays: 90, extraFrameAncestors: [] });
  app.node.setContext('mcpSandbox', { extraFrameAncestors: [] });
  app.node.setContext('ragIngestion', {
    additionalCorsOrigins: '',
    lambdaMemorySize: 10240,
    lambdaTimeout: 900,
    embeddingModel: 'amazon.titan-embed-text-v2',
    vectorDimension: 1024,
    vectorDistanceMetric: 'cosine',
  });
}

describe('SPA distribution frame-src CSP', () => {
  const PREV = process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN;

  afterAll(() => {
    if (PREV === undefined) {
      delete process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN;
    } else {
      process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = PREV;
    }
  });

  it('allows both the artifacts and mcp-sandbox origins on a domained deploy', () => {
    delete process.env.CDK_FRONTEND_CERTIFICATE_ARN;
    delete process.env.CDK_ARTIFACTS_CERTIFICATE_ARN;
    delete process.env.CDK_MCP_SANDBOX_CERTIFICATE_ARN;
    process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED_CF_CERT;

    const app = new cdk.App();
    seedRequiredContext(app);
    const config = loadConfig(app);
    mockSsmContext(app, config);

    const stack = new PlatformStack(app, 'FrameSrcCspPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    const template = Template.fromStack(stack);

    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: {
        Name: 'test-project-frontend-headers',
        SecurityHeadersConfig: {
          ContentSecurityPolicy: {
            ContentSecurityPolicy:
              "frame-src 'self' https://artifacts.example.com https://mcp-sandbox.example.com",
            Override: true,
          },
        },
      },
    });
  });
});
