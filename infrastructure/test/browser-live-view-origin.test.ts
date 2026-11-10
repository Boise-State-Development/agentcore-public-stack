/**
 * The browser sign-in viewer must never share an origin with MCP Apps.
 *
 * `live-view.html` is where a user types a password into a browser running in
 * our AWS account. The mcp-sandbox origin runs untrusted MCP App HTML. Two
 * frames of one origin on one page can script each other, so the viewer has
 * its own distribution (`BrowserLiveViewConstruct`), its own bucket, and no
 * custom domain — which is also why it needs no certificate or DNS record.
 *
 * These tests pin each half of that: the two origins differ in every
 * deployment shape, each distribution can read only its own bucket, the
 * repo's two asset directories hold only their own pages, the origin reaches
 * app-api (which returns it on the mint response) and the SPA's `frame-src`,
 * and the AgentCore Runtime's environment is unchanged.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';
import { loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const SHARED_CF_CERT = 'arn:aws:acm:us-east-1:123456789012:certificate/shared-wildcard';
const LIVE_VIEW_COMMENT = 'test-project-browser-live-view-cdn';
const SANDBOX_COMMENT = 'test-project-mcp-sandbox-cdn';

const ENV_KEYS = [
  'CDK_CLOUDFRONT_CERTIFICATE_ARN',
  'CDK_FRONTEND_CERTIFICATE_ARN',
  'CDK_ARTIFACTS_CERTIFICATE_ARN',
  'CDK_MCP_SANDBOX_CERTIFICATE_ARN',
  'CDK_ALLOW_NO_CORS_ORIGINS',
];

function seedRequiredContext(app: cdk.App, domained: boolean): void {
  app.node.setContext('projectPrefix', 'test-project');
  app.node.setContext('awsRegion', MOCK_REGION);
  app.node.setContext('awsAccount', MOCK_ACCOUNT);
  app.node.setContext('vpcCidr', '10.0.0.0/16');
  app.node.setContext('production', false);
  app.node.setContext('retainDataOnDelete', false);
  if (domained) {
    app.node.setContext('domainName', 'example.com');
    app.node.setContext('infrastructureHostedZoneDomain', 'example.com');
  }
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

function synth(domained: boolean): Template {
  for (const key of ENV_KEYS) delete process.env[key];
  if (domained) process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED_CF_CERT;
  // A domain-less stack has no SPA origin to allow; config refuses that unless told.
  else process.env.CDK_ALLOW_NO_CORS_ORIGINS = 'true';
  const app = new cdk.App();
  seedRequiredContext(app, domained);
  const config = loadConfig(app);
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, domained ? 'LiveViewDomained' : 'LiveViewDomainless', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

type Resource = { Type: string; Properties: any };

function distributionByComment(template: Template, comment: string): [string, Resource] {
  const found = Object.entries(
    template.findResources('AWS::CloudFront::Distribution') as Record<string, Resource>,
  ).filter(([, r]) => r.Properties.DistributionConfig.Comment === comment);
  expect(found).toHaveLength(1);
  return found[0];
}

/** The `https://` + GetAtt DomainName join CDK renders for a default-domain origin. */
function defaultDomainOrigin(logicalId: string): unknown {
  return { 'Fn::Join': ['', ['https://', { 'Fn::GetAtt': [logicalId, 'DomainName'] }]] };
}

/** Every container/Runtime environment entry named `name`, across the template. */
function envValues(template: Template, name: string): Array<{ type: string; value: unknown }> {
  const out: Array<{ type: string; value: unknown }> = [];
  for (const resource of Object.values(template.toJSON().Resources as Record<string, Resource>)) {
    if (resource.Type === 'AWS::ECS::TaskDefinition') {
      for (const container of resource.Properties.ContainerDefinitions ?? []) {
        for (const entry of container.Environment ?? []) {
          if (entry.Name === name) out.push({ type: resource.Type, value: entry.Value });
        }
      }
    }
    const runtimeEnv = resource.Properties?.EnvironmentVariables;
    if (runtimeEnv && typeof runtimeEnv === 'object' && name in runtimeEnv) {
      out.push({ type: resource.Type, value: runtimeEnv[name] });
    }
  }
  return out;
}

/** The bucket logical id a distribution's (single) S3 origin reads. */
function originBucket(dist: Resource): string {
  const origins = dist.Properties.DistributionConfig.Origins;
  expect(origins).toHaveLength(1);
  return origins[0].DomainName['Fn::GetAtt'][0];
}

describe('browser sign-in viewer origin', () => {
  const PREV = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  afterAll(() => {
    for (const [key, value] of Object.entries(PREV)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  describe('on a domained deploy', () => {
    let template: Template;
    beforeAll(() => {
      template = synth(true);
    });

    it('serves the viewer from a different host than the MCP App proxy', () => {
      const [, sandbox] = distributionByComment(template, SANDBOX_COMMENT);
      const [liveViewId, liveView] = distributionByComment(template, LIVE_VIEW_COMMENT);

      expect(sandbox.Properties.DistributionConfig.Aliases).toEqual(['mcp-sandbox.example.com']);
      // No alias at all: the viewer lives on CloudFront's default domain, so
      // it can never resolve to a host an MCP App is served from, and needs
      // no certificate or Route53 record.
      expect(liveView.Properties.DistributionConfig.Aliases).toBeUndefined();
      expect(liveView.Properties.DistributionConfig.ViewerCertificate?.AcmCertificateArn).toBeUndefined();

      const [appApi] = envValues(template, 'BROWSER_LIVE_VIEW_ORIGIN');
      expect(appApi.value).toEqual(defaultDomainOrigin(liveViewId));
      const [runtime] = envValues(template, 'AGENTCORE_MCP_APPS_SANDBOX_ORIGIN');
      expect(runtime.value).toBe('https://mcp-sandbox.example.com');
      expect(appApi.value).not.toEqual(runtime.value);
    });

    it('creates no DNS record for the viewer', () => {
      const records = Object.values(
        template.findResources('AWS::Route53::RecordSet') as Record<string, Resource>,
      ).map((r) => String(r.Properties.Name));
      expect(records.some((name) => name.includes('live-view'))).toBe(false);
    });

    it("adds the viewer's origin to the SPA's frame-src beside the sandbox origin", () => {
      const [liveViewId] = distributionByComment(template, LIVE_VIEW_COMMENT);
      const policies = Object.values(
        template.findResources('AWS::CloudFront::ResponseHeadersPolicy') as Record<string, Resource>,
      ).filter((p) => p.Properties.ResponseHeadersPolicyConfig.Name === 'test-project-frontend-headers');
      expect(policies).toHaveLength(1);
      const csp =
        policies[0].Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig.ContentSecurityPolicy;
      expect(csp.Override).toBe(true);
      expect(csp.ContentSecurityPolicy).toEqual({
        'Fn::Join': [
          '',
          [
            "frame-src 'self' https://artifacts.example.com https://mcp-sandbox.example.com https://",
            { 'Fn::GetAtt': [liveViewId, 'DomainName'] },
          ],
        ],
      });
    });
  });

  describe('on a domain-less deploy', () => {
    let template: Template;
    beforeAll(() => {
      template = synth(false);
    });

    it('still gives the viewer and the proxy two different distributions', () => {
      const [sandboxId] = distributionByComment(template, SANDBOX_COMMENT);
      const [liveViewId] = distributionByComment(template, LIVE_VIEW_COMMENT);
      expect(liveViewId).not.toBe(sandboxId);

      const [appApi] = envValues(template, 'BROWSER_LIVE_VIEW_ORIGIN');
      const [runtime] = envValues(template, 'AGENTCORE_MCP_APPS_SANDBOX_ORIGIN');
      expect(appApi.value).toEqual(defaultDomainOrigin(liveViewId));
      expect(runtime.value).toEqual(defaultDomainOrigin(sandboxId));
    });

    it('gives each distribution its own bucket, so neither can serve the other page', () => {
      const [, sandbox] = distributionByComment(template, SANDBOX_COMMENT);
      const [, liveView] = distributionByComment(template, LIVE_VIEW_COMMENT);
      expect(originBucket(liveView)).not.toBe(originBucket(sandbox));
    });

    it('locks both to the same frame-ancestors by sharing one CSP function', () => {
      const [, sandbox] = distributionByComment(template, SANDBOX_COMMENT);
      const [, liveView] = distributionByComment(template, LIVE_VIEW_COMMENT);
      const fn = (d: Resource) =>
        d.Properties.DistributionConfig.DefaultCacheBehavior.FunctionAssociations;
      expect(fn(liveView)).toEqual(fn(sandbox));
      expect(fn(liveView)).toHaveLength(1);
      expect(fn(liveView)[0].EventType).toBe('viewer-response');
    });

    it('keeps the viewer origin out of the AgentCore Runtime environment', () => {
      const types = envValues(template, 'BROWSER_LIVE_VIEW_ORIGIN').map((e) => e.type);
      expect(types).toEqual(['AWS::ECS::TaskDefinition']);
    });
  });

  describe('asset directories', () => {
    const assets = path.join(__dirname, '..', 'assets');
    const tracked = (dir: string) =>
      fs.readdirSync(path.join(assets, dir)).filter((name) => name !== 'dcvjs');

    it('holds no viewer file in the MCP App bucket', () => {
      expect(tracked('mcp-sandbox').sort()).toEqual(['csp-function.js', 'proxy.html', 'proxy.js']);
      expect(fs.existsSync(path.join(assets, 'mcp-sandbox', 'dcvjs'))).toBe(false);
    });

    it('holds no MCP App file in the viewer bucket', () => {
      expect(tracked('browser-live-view').sort()).toEqual(['live-view.html', 'live-view.js']);
    });
  });
});
