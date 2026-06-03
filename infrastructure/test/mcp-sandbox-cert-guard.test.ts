/**
 * Guard test for McpSandboxDistributionConstruct's domain/cert pairing.
 *
 * Regression cover for the #396 stack-consolidation bug: the
 * `CDK_MCP_SANDBOX_CERTIFICATE_ARN` deploy var was dropped from the
 * consolidated platform.yml, so the construct silently fell back to the
 * CloudFront default domain and created no Route53 ALIAS — the SPA then
 * framed a nonexistent `mcp-sandbox.{domain}` host (NXDOMAIN) and every
 * MCP App failed to load. The construct now fails loudly on the
 * domain-set-but-cert-missing case while preserving the documented
 * domain-less fallback (synth/unit tests, local).
 */
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { McpSandboxDistributionConstruct } from '../lib/constructs/mcp-sandbox/mcp-sandbox-distribution-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const CERT = 'arn:aws:acm:us-east-1:123456789012:certificate/test';

function buildConstruct(overrides: Parameters<typeof createMockConfig>[0]): void {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const bucket = new s3.Bucket(stack, 'ShellBucket');
  new McpSandboxDistributionConstruct(stack, 'McpSandbox', { config, bucket });
}

describe('McpSandboxDistributionConstruct — domain/cert guard', () => {
  it('throws when a domain is configured but the cert is missing', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        mcpSandbox: { extraFrameAncestors: [] }, // no certificateArn
      }),
    ).toThrow(/MCP sandbox proxy requires an ACM certificate/);
  });

  it('names the missing deploy var and the affected subdomain in the error', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        mcpSandbox: { extraFrameAncestors: [] },
      }),
    ).toThrow(/CDK_MCP_SANDBOX_CERTIFICATE_ARN.*mcp-sandbox\.example\.com/s);
  });

  it('does NOT throw for a domain-less stack (CloudFront default fallback)', () => {
    expect(() =>
      buildConstruct({
        mcpSandbox: { extraFrameAncestors: [] }, // no domain, no cert
      }),
    ).not.toThrow();
  });

  it('does NOT throw when both a domain and cert are configured', () => {
    expect(() =>
      buildConstruct({
        domainName: 'example.com',
        infrastructureHostedZoneDomain: 'example.com',
        mcpSandbox: { extraFrameAncestors: [], certificateArn: CERT },
      }),
    ).not.toThrow();
  });
});
