import { Template, Match } from 'aws-cdk-lib/assertions';
import {
  McpSandboxStack,
  buildMcpSandboxFrameAncestors,
  buildMcpSandboxProxyCsp,
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

  test('ResponseHeadersPolicy CSP locks frame-ancestors to the SPA origin only', () => {
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          ContentSecurityPolicy: Match.objectLike({
            Override: true,
            ContentSecurityPolicy: Match.stringLikeRegexp(
              'frame-ancestors https://test\\.example\\.com http://localhost:4200',
            ),
          }),
          StrictTransportSecurity: Match.objectLike({ Override: true }),
        }),
      }),
    });
  });

  test('does NOT set legacy X-Frame-Options (frame-ancestors is the control)', () => {
    const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const policy = Object.values(policies)[0] as any;
    const security = policy.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig;
    expect(security.FrameOptions).toBeUndefined();
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

  test('domain-less config denies all framing (frame-ancestors none)', () => {
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
    t.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          ContentSecurityPolicy: Match.objectLike({
            ContentSecurityPolicy: Match.stringLikeRegexp("frame-ancestors 'none'"),
          }),
        }),
      }),
    });
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

describe('buildMcpSandboxProxyCsp', () => {
  test('carries the given frame-ancestors + non-overridable hardening', () => {
    const csp = buildMcpSandboxProxyCsp('https://alpha.example.com');
    expect(csp).toContain('frame-ancestors https://alpha.example.com');
    // Hardening directives that proxy.html shouldn't ever want to relax,
    // even though most of the rest is intentionally broad.
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-src 'none'");
    expect(csp).toContain("connect-src 'self'");
  });

  test('matches the ext-apps reference defaults so typical bundled Apps run', () => {
    // The inner App iframe inherits this CSP (CSP3 local-scheme rule applies
    // to srcdoc / blob: / document.write()-populated about:blank alike). The
    // modelcontextprotocol/ext-apps basic-host reference ships its outer CSP
    // with these tokens baked in for the same reason — bundled-App inline
    // scripts/styles/eval need to actually run. See mcp-sandbox-stack.ts
    // docstring for the security rationale.
    const csp = buildMcpSandboxProxyCsp("'none'");
    expect(csp).toContain(
      "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:",
    );
    expect(csp).toContain("style-src 'self' 'unsafe-inline' blob: data:");
    expect(csp).toContain("worker-src 'self' blob:");
    expect(csp).toContain("default-src 'self' 'unsafe-inline'");
  });
});

describe('subdomain decision', () => {
  test('is the documented "mcp-sandbox" label (TBD resolved in PR #1)', () => {
    expect(MCP_SANDBOX_SUBDOMAIN_LABEL).toBe('mcp-sandbox');
  });
});
