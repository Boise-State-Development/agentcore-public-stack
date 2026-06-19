/**
 * End-to-end regression cover for the shared CloudFront certificate.
 *
 * Encodes the forked-repo first-deploy scenario that previously failed:
 * a domain is configured and the operator supplies a SINGLE shared
 * CloudFront cert (CDK_CLOUDFRONT_CERTIFICATE_ARN) — no per-origin
 * frontend/artifacts/mcp-sandbox cert vars. This must drive the real
 * `loadConfig` resolution (shared → every CloudFront section) and then
 * synthesize PlatformStack cleanly, with all three CloudFront origins on
 * their custom domains. Before the shared-cert fallback existed, the
 * artifacts and mcp-sandbox guards would abort synth because their
 * section-specific cert was empty.
 *
 * Unlike platform-stack.test.ts (which hands constructs a pre-built
 * mock config), this test exercises `loadConfig` itself so the
 * env-var → resolution → construct seam is covered as one flow.
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

describe('PlatformStack — shared CloudFront certificate (forked first-deploy)', () => {
  const PREV = process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN;

  afterAll(() => {
    if (PREV === undefined) {
      delete process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN;
    } else {
      process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = PREV;
    }
  });

  it('synthesizes with only the shared cert set (no per-origin cert vars)', () => {
    delete process.env.CDK_FRONTEND_CERTIFICATE_ARN;
    delete process.env.CDK_ARTIFACTS_CERTIFICATE_ARN;
    delete process.env.CDK_MCP_SANDBOX_CERTIFICATE_ARN;
    process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED_CF_CERT;

    const app = new cdk.App();
    seedRequiredContext(app);

    // Real loadConfig resolution: the shared cert must populate all three
    // CloudFront sections even though none were set individually.
    const config = loadConfig(app);
    expect(config.frontend.certificateArn).toBe(SHARED_CF_CERT);
    expect(config.artifacts.certificateArn).toBe(SHARED_CF_CERT);
    expect(config.mcpSandbox.certificateArn).toBe(SHARED_CF_CERT);

    mockSsmContext(app, config);

    // No throw == the artifacts/mcp-sandbox cert guards were satisfied by
    // the shared fallback. Three distributions on their custom domains.
    const stack = new PlatformStack(app, 'SharedCertPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::CloudFront::Distribution', 3);
  });
});
