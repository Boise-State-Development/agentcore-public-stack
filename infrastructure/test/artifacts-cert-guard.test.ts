/**
 * Guard test for ArtifactsDistributionConstruct's domain/cert pairing.
 *
 * Companion to mcp-sandbox-cert-guard.test.ts. Before this guard existed
 * the construct dereferenced `config.artifacts.certificateArn!` (non-null
 * assertion) with no check and a stale comment claiming config.ts had
 * "already enforced" the cert — it had not. A domained deploy that omitted
 * the artifacts cert therefore handed `undefined` to
 * `acm.Certificate.fromCertificateArn`, failing with an opaque CDK error
 * instead of an actionable one. The construct now fails loudly on the
 * domain-set-but-cert-missing case, mirroring the MCP sandbox guard, and
 * names both the section-specific and the shared cert vars.
 *
 * The artifacts cert is resolved in config.ts, where each CloudFront
 * section falls back to the shared CDK_CLOUDFRONT_CERTIFICATE_ARN when its
 * own ARN is unset — so this guard only trips when neither was supplied.
 */
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { ArtifactsDistributionConstruct } from '../lib/constructs/artifacts/artifacts-distribution-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const CERT = 'arn:aws:acm:us-east-1:123456789012:certificate/test';

function buildConstruct(overrides: Parameters<typeof createMockConfig>[0]): void {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  // The render Lambda + Function URL the distribution fronts. Created
  // unconditionally; the guard under test throws before it is consumed in
  // the failure cases, and the positive case exercises it for real.
  const renderFn = new lambda.Function(stack, 'RenderFn', {
    runtime: lambda.Runtime.NODEJS_20_X,
    handler: 'index.handler',
    code: lambda.Code.fromInline('exports.handler = async () => ({});'),
  });
  const renderFunctionUrl = renderFn.addFunctionUrl({
    authType: lambda.FunctionUrlAuthType.AWS_IAM,
  });
  new ArtifactsDistributionConstruct(stack, 'Artifacts', {
    config,
    renderFunctionUrl,
    frameAncestors: "'none'",
  });
}

describe('ArtifactsDistributionConstruct — domain/cert guard', () => {
  it('throws when a domain is configured but the artifacts cert is missing', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        artifacts: { retentionDays: 90, extraFrameAncestors: [] }, // no certificateArn
      }),
    ).toThrow(/Artifacts iframe origin requires an ACM certificate/);
  });

  it('names both the section-specific and shared cert vars and the affected subdomain', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        artifacts: { retentionDays: 90, extraFrameAncestors: [] },
      }),
    ).toThrow(
      /CDK_ARTIFACTS_CERTIFICATE_ARN.*CDK_CLOUDFRONT_CERTIFICATE_ARN.*artifacts\.example\.com/s,
    );
  });

  it('does NOT throw when both a domain and an artifacts cert are configured', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: CERT },
      }),
    ).not.toThrow();
  });

  it('does NOT throw for a domain-less stack (CloudFront default domain fallback)', () => {
    // Regression: artifacts previously had no domain-less branch and crashed
    // with `Cannot read properties of undefined (reading 'startsWith')` from
    // fromCertificateArn(undefined). It now falls back to the CloudFront
    // default domain like McpSandboxDistributionConstruct.
    expect(() =>
      buildConstruct({
        artifacts: { retentionDays: 90, extraFrameAncestors: [] }, // no domain, no cert
      }),
    ).not.toThrow();
  });
});
