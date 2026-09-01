/**
 * Regression cover for the `/api/*` behavior's response-headers policy.
 *
 * app-api is served from the SAME CloudFront distribution — and therefore the
 * same browser origin — as the Angular SPA. The `/api/*` behavior originally
 * carried no `responseHeadersPolicy` at all, so API responses shipped with
 * neither `X-Content-Type-Options: nosniff` nor a `Content-Security-Policy`
 * while `GET /` (the SPA) had both. That gap was the read half of a stored-XSS
 * privilege escalation: a user-uploaded skill resource served as
 * `text/html; charset=utf-8` with `Content-Disposition: inline` parsed as a
 * top-level document on the SPA's origin and executed its inline `<script>`
 * with the victim admin's session cookie and CSRF token.
 *
 * The app-api routes now harden their own responses, but this policy is the
 * origin-wide backstop: it means no future route can regress the whole origin
 * by forgetting a header. Both halves are asserted here.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

/** Seed every context value loadConfig requires. */
function seedRequiredContext(app: cdk.App): void {
  app.node.setContext('projectPrefix', 'test-project');
  app.node.setContext('awsRegion', MOCK_REGION);
  app.node.setContext('awsAccount', MOCK_ACCOUNT);
  app.node.setContext('vpcCidr', '10.0.0.0/16');
  app.node.setContext('production', false);
  app.node.setContext('retainDataOnDelete', false);
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

function synth(): Template {
  const app = new cdk.App();
  seedRequiredContext(app);
  const config = loadConfig(app);
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'ApiHeadersPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  return Template.fromStack(stack);
}

describe('/api/* security response headers', () => {
  it('defines a dedicated api-headers policy with nosniff and an inert CSP', () => {
    synth().hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: {
        Name: 'test-project-api-headers',
        SecurityHeadersConfig: {
          // Without nosniff a browser may re-sniff `<html>` bytes served as
          // octet-stream back into text/html.
          ContentTypeOptions: { Override: true },
          // `default-src 'none'` blocks inline and external script, so an
          // HTML-typed API body cannot reach the SPA's session even if a
          // browser renders it.
          ContentSecurityPolicy: {
            ContentSecurityPolicy: "default-src 'none'; frame-ancestors 'none'",
            Override: true,
          },
          FrameOptions: { FrameOption: 'DENY', Override: true },
          ReferrerPolicy: { ReferrerPolicy: 'no-referrer', Override: true },
        },
      },
    });
  });

  it('attaches the policy to the /api/* cache behavior', () => {
    const template = synth();
    const distributions = template.findResources('AWS::CloudFront::Distribution');

    // The SPA distribution is the one carrying the /api/* behavior.
    const spa = Object.values(distributions).find((d) =>
      (d.Properties?.DistributionConfig?.CacheBehaviors ?? []).some(
        (b: { PathPattern?: string }) => b.PathPattern === '/api/*',
      ),
    );
    expect(spa).toBeDefined();

    const apiBehavior = spa!.Properties.DistributionConfig.CacheBehaviors.find(
      (b: { PathPattern?: string }) => b.PathPattern === '/api/*',
    );
    expect(apiBehavior.ResponseHeadersPolicyId).toBeDefined();

    // ...and it is the api policy, not the SPA's (whose CSP only sets
    // frame-src and would leave script-src unconstrained on /api/*).
    const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const apiPolicyLogicalId = Object.entries(policies).find(
      ([, p]) =>
        p.Properties?.ResponseHeadersPolicyConfig?.Name === 'test-project-api-headers',
    )?.[0];
    expect(apiPolicyLogicalId).toBeDefined();
    expect(apiBehavior.ResponseHeadersPolicyId).toEqual({ Ref: apiPolicyLogicalId });
  });
});
