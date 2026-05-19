import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  McpSandboxStack,
  buildMcpSandboxFrameAncestors,
  loadMcpSandboxCspFunctionCode,
  MCP_SANDBOX_SUBDOMAIN_LABEL,
} from '../lib/mcp-sandbox-stack';
import { createMockConfig, createMockApp, mockEnv } from './helpers/mock-config';

describe('McpSandboxStack', () => {
  // Default mock config has a domainName but no mcpSandbox.certificateArn, so
  // the stack synthesizes on the CloudFront default domain (no ACM import, no
  // Route53 lookup) while still deriving the real frame-ancestors from the
  // domain — exactly the unit/synth path.
  let template: Template;

  beforeEach(() => {
    const config = createMockConfig({
      domainName: 'test.example.com',
      mcpSandbox: {
        enabled: true,
        extraFrameAncestors: ['http://localhost:4200'],
      },
    });
    const app = createMockApp(config, ['McpSandboxStack']);
    const stack = new McpSandboxStack(app, 'TestMcpSandboxStack', {
      config,
      env: mockEnv(config),
    });
    template = Template.fromStack(stack);
  });

  test('synthesizes without errors', () => {
    expect(template.toJSON()).toBeDefined();
  });

  test('creates a private, encrypted S3 bucket that blocks all public access', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: Match.objectLike({
              SSEAlgorithm: Match.anyValue(),
            }),
          }),
        ]),
      },
    });
  });

  test('creates exactly one CloudFront distribution', () => {
    template.resourceCountIs('AWS::CloudFront::Distribution', 1);
  });

  test('CloudFront serves proxy.html as the default root object', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultRootObject: 'proxy.html',
      }),
    });
  });

  test('ResponseHeadersPolicy keeps non-CSP security headers but does NOT emit CSP (CSP is now per-request via the CloudFront Function)', () => {
    // CSP via the response-headers-policy would be a SECOND `Content-
    // Security-Policy` header alongside the dynamic one from the CFN;
    // browsers intersect them, which would silently re-deny anything an
    // App legitimately declared in `_meta.ui.csp`. So the policy carries
    // only the headers that don't vary per resource.
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          StrictTransportSecurity: Match.objectLike({ Override: true }),
          ReferrerPolicy: Match.objectLike({ Override: true }),
          ContentTypeOptions: Match.objectLike({ Override: true }),
        }),
      }),
    });

    const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const policy = Object.values(policies)[0] as any;
    const security = policy.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig;
    expect(security.ContentSecurityPolicy).toBeUndefined();
  });

  test('does NOT set legacy X-Frame-Options (frame-ancestors via the CSP function is the control)', () => {
    const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const policy = Object.values(policies)[0] as any;
    const security = policy.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig;
    expect(security.FrameOptions).toBeUndefined();
  });

  test('creates exactly one CloudFront Function for dynamic CSP, on the JS_2_0 runtime', () => {
    template.resourceCountIs('AWS::CloudFront::Function', 1);
    template.hasResourceProperties('AWS::CloudFront::Function', {
      FunctionConfig: Match.objectLike({
        Runtime: 'cloudfront-js-2.0',
      }),
    });
  });

  test('CFN function body has the frame-ancestors placeholder substituted with the real source list', () => {
    const fns = template.findResources('AWS::CloudFront::Function');
    const fn = Object.values(fns)[0] as any;
    const code = fn.Properties.FunctionCode as string;
    expect(code).toContain('https://test.example.com http://localhost:4200');
    // The substitutable JS literal must be gone; the bare token still
    // appears in a top-of-file comment and that's intentional.
    expect(code).not.toContain("'__INJECT_FRAME_ANCESTORS__'");
  });

  test('CFN function comment fits within the AWS-enforced 128-char limit', () => {
    // CloudFront::Function.FunctionConfig.Comment maxes at 128 chars and
    // CloudFormation rejects the create with a 400 if exceeded — see
    // alpha deploy 2026-05-19 which rolled back on this. Catching at
    // synth time prevents another wasted deploy round-trip.
    const fns = template.findResources('AWS::CloudFront::Function');
    const fn = Object.values(fns)[0] as any;
    const comment = fn.Properties.FunctionConfig.Comment as string;
    expect(comment.length).toBeLessThanOrEqual(128);
  });

  test('CFN function is wired to viewer-response on the default behavior', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          FunctionAssociations: Match.arrayWith([
            Match.objectLike({
              EventType: 'viewer-response',
              FunctionARN: Match.anyValue(),
            }),
          ]),
        }),
      }),
    });
  });

  test('bakes the shell in via a BucketDeployment with CloudFront invalidation', () => {
    template.resourceCountIs('Custom::CDKBucketDeployment', 1);
    template.hasResourceProperties('Custom::CDKBucketDeployment', {
      DistributionPaths: ['/*'],
    });
  });

  test('writes the one-way /mcp-sandbox/origin SSM export', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/mcp-sandbox/origin',
      Type: 'String',
    });
  });

  test('does not create a Route53 record without a custom domain cert', () => {
    template.resourceCountIs('AWS::Route53::RecordSet', 0);
  });

  test('domain-less config bakes "frame-ancestors none" into the CSP function code', () => {
    const config = createMockConfig({
      domainName: undefined,
      mcpSandbox: { enabled: true, extraFrameAncestors: [] },
    });
    const app = createMockApp(config, ['McpSandboxStack']);
    const stack = new McpSandboxStack(app, 'NoDomainMcpSandboxStack', {
      config,
      env: mockEnv(config),
    });
    const t = Template.fromStack(stack);
    const fns = t.findResources('AWS::CloudFront::Function');
    const fn = Object.values(fns)[0] as any;
    const code = fn.Properties.FunctionCode as string;
    // JSON.stringify wraps single-quoted CSP keywords in double quotes —
    // the resulting JS literal is `"'none'"` (no escaping needed since
    // JSON.stringify never emits backslashed single quotes).
    expect(code).toContain('var FRAME_ANCESTORS = "\'none\'";');
  });
});

describe('buildMcpSandboxFrameAncestors', () => {
  test('prod: SPA origin derived from the domain', () => {
    expect(buildMcpSandboxFrameAncestors('alpha.example.com', [])).toBe(
      'https://alpha.example.com',
    );
  });

  test('prod + extra origins (e.g. local SPA pointed at this env)', () => {
    expect(
      buildMcpSandboxFrameAncestors('alpha.example.com', ['http://localhost:4200']),
    ).toBe('https://alpha.example.com http://localhost:4200');
  });

  test('no domain, no extras → deny all framing', () => {
    expect(buildMcpSandboxFrameAncestors(undefined, [])).toBe("'none'");
  });

  test('extras only (domain-less local stack)', () => {
    expect(buildMcpSandboxFrameAncestors(undefined, ['http://localhost:4200'])).toBe(
      'http://localhost:4200',
    );
  });

  test('blank extras are filtered out, never widening to *', () => {
    expect(buildMcpSandboxFrameAncestors('alpha.example.com', ['', '  '])).toBe(
      'https://alpha.example.com',
    );
  });
});

describe('loadMcpSandboxCspFunctionCode', () => {
  test('substitutes the FRAME_ANCESTORS placeholder with the real source list (as a JSON-escaped JS literal)', () => {
    const code = loadMcpSandboxCspFunctionCode('https://alpha.example.com');
    expect(code).toContain('var FRAME_ANCESTORS = "https://alpha.example.com";');
    // The replaceable quoted literal must be gone; the bare token may
    // still appear in a comment, which is fine.
    expect(code).not.toContain("'__INJECT_FRAME_ANCESTORS__'");
  });

  test('preserves the runtime helpers (sanitize / build / parse / handler)', () => {
    const code = loadMcpSandboxCspFunctionCode("'none'");
    expect(code).toContain('function sanitizeCspDomains(');
    expect(code).toContain('function buildCspHeader(');
    expect(code).toContain('function parseCspParam(');
    expect(code).toContain('function handler(');
  });

  test('handles the deny-all frame-ancestors value without producing invalid JS', () => {
    const code = loadMcpSandboxCspFunctionCode("'none'");
    // JSON.stringify yields `"'none'"` (double-quoted, inner single
    // quotes unescaped) — a valid JS string literal that decodes back to
    // the CSP source `'none'` at runtime. Without this escaping the
    // naive replace would produce `''none''`, a syntax error.
    expect(code).toContain('var FRAME_ANCESTORS = "\'none\'";');
  });

  test('handles multiple space-separated source list entries (the common production shape)', () => {
    const code = loadMcpSandboxCspFunctionCode(
      'https://alpha.example.com http://localhost:4200',
    );
    expect(code).toContain(
      'var FRAME_ANCESTORS = "https://alpha.example.com http://localhost:4200";',
    );
  });
});

describe('subdomain decision', () => {
  test('is the documented "mcp-sandbox" label (TBD resolved in PR #1)', () => {
    expect(MCP_SANDBOX_SUBDOMAIN_LABEL).toBe('mcp-sandbox');
  });
});
