import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';
import {
  AppConfig,
  applyStandardTags,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from './config';

/**
 * The full JS string literal in `assets/mcp-sandbox/csp-function.js` that
 * we substitute at synth time. Matching the *quoted* literal (not the
 * inner identifier) lets us replace it with `JSON.stringify(value)`,
 * which handles quote-escaping correctly for any `frame-ancestors` source
 * list — including `'none'` (which would otherwise produce `''none''`,
 * a JS syntax error).
 *
 * The CFN unit tests don't depend on the substitution: they pass
 * `frameAncestors` directly to `buildCspHeader`, so the unsubstituted
 * source file is valid JS for `require()` to load.
 */
const FRAME_ANCESTORS_PLACEHOLDER_LITERAL = "'__INJECT_FRAME_ANCESTORS__'";

/**
 * Load the dynamic-CSP CloudFront Function source and inject the real
 * `frame-ancestors` source list as a properly-escaped JS string literal.
 * Asserts the placeholder is present exactly once so a future refactor
 * that loses the marker fails loudly at synth, not at edge runtime.
 *
 * Exported for unit testing.
 */
export function loadMcpSandboxCspFunctionCode(frameAncestors: string): string {
  const filePath = path.resolve(
    __dirname,
    '..',
    'assets',
    'mcp-sandbox',
    'csp-function.js',
  );
  const source = fs.readFileSync(filePath, 'utf8');
  const occurrences = source.split(FRAME_ANCESTORS_PLACEHOLDER_LITERAL).length - 1;
  if (occurrences !== 1) {
    throw new Error(
      `Expected exactly one occurrence of ${FRAME_ANCESTORS_PLACEHOLDER_LITERAL} in csp-function.js, found ${occurrences}. Did the marker get renamed or duplicated?`,
    );
  }
  return source.replace(FRAME_ANCESTORS_PLACEHOLDER_LITERAL, JSON.stringify(frameAncestors));
}

export interface McpSandboxStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * The subdomain label for the MCP Apps sandbox-proxy origin.
 *
 * Decision (the scoping doc explicitly leaves this "TBD in PR #1"): use
 * `mcp-sandbox`, matching the working name in
 * docs/kaizen/scoping/mcp-apps-host-renderer.md and paralleling the existing
 * sibling iframe origin `artifacts.{domain}`. This single constant is the
 * source of truth — it must stay in sync with the CDK_MCP_SANDBOX_* workflow
 * env vars and the cors-deployment skill notes.
 */
export const MCP_SANDBOX_SUBDOMAIN_LABEL = 'mcp-sandbox';

/**
 * Build the CSP `frame-ancestors` source list for the proxy origin.
 *
 * The proxy may ONLY be embedded by the SPA (`ai.client`) origin, which is
 * `https://{domainName}` plus any explicitly-allowed extras (e.g.
 * http://localhost:4200 for a local SPA pointed at this env). This is the
 * security-critical control for PR #1.
 *
 * Falls back to `'none'` (deny all framing) when there is no SPA origin to
 * permit — keeps the stack synthesizable for unit/synth tests and
 * domain-less local stacks without ever silently allowing `*`.
 *
 * Exported so the value is unit-testable directly and the stack body has a
 * single source of truth.
 */
export function buildMcpSandboxFrameAncestors(
  domainName: string | undefined,
  extraFrameAncestors: string[],
): string {
  const sources: string[] = [];
  if (domainName) {
    sources.push(`https://${domainName}`);
  }
  for (const extra of extraFrameAncestors) {
    const trimmed = extra.trim();
    if (trimmed) {
      sources.push(trimmed);
    }
  }
  return sources.length > 0 ? sources.join(' ') : `'none'`;
}

/**
 * MCP Apps host renderer — Sandbox Proxy origin.
 *
 * PR #1 of docs/kaizen/scoping/mcp-apps-host-renderer.md.
 *
 * Provisions a dedicated cross-origin static origin that serves a single
 * shell, `proxy.html`, at `mcp-sandbox.{domainName}`:
 *
 *   - S3 bucket (private, OAC-only) holding proxy.html + proxy.js
 *   - BucketDeployment that bakes those assets in at deploy time (the stack
 *     is self-contained — no separate asset-sync step)
 *   - CloudFront distribution terminating TLS and stamping the CSP
 *     (`frame-ancestors` locked to the SPA origin)
 *   - Route53 A record for the subdomain (when a custom domain + cert are
 *     configured)
 *
 * The proxy is the OUTER half of the spec's Sandbox Proxy pattern; it itself
 * creates the inner content iframe via `srcdoc` (see assets/mcp-sandbox/).
 *
 * INERT BY DESIGN: this stack writes exactly one SSM export
 * (`/{prefix}/mcp-sandbox/origin`) that nothing consumes until the frontend
 * `<mcp-app-frame>` lands (PR #4) and the whole host renderer stays gated
 * behind `MCP_APPS_HOST_ENABLED` until PR #7. Deploying it changes nothing
 * user-facing.
 *
 * Cross-stack contract: reads NOTHING from other stacks (cert ARN comes from
 * config, the hosted zone via Route53 lookup). One-way SSM publish only —
 * deploy tier 1, parallel-safe with Artifacts / RAG / Gateway / Fine-Tuning.
 */
export class McpSandboxStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  public readonly proxyOrigin: string;

  constructor(scope: Construct, id: string, props: McpSandboxStackProps) {
    super(scope, id, props);

    const { config } = props;
    applyStandardTags(this, config);

    // Custom domain + cert + Route53 are attached only when BOTH a domain and
    // an ACM cert are configured (config.ts enforces both, plus the hosted
    // zone, whenever the stack is *enabled*). Keeping it conditional — the
    // FrontendStack pattern — lets the stack still synthesize on the
    // CloudFront default domain for unit/synth tests and domain-less local
    // stacks, while a real deploy always has the full custom-domain path.
    const domainName = config.domainName;
    const certificateArn = config.mcpSandbox.certificateArn;
    const useCustomDomain = Boolean(domainName && certificateArn);
    const proxySubdomain = domainName
      ? `${MCP_SANDBOX_SUBDOMAIN_LABEL}.${domainName}`
      : undefined;

    // The SPA (ai.client) origin is the ONLY origin allowed to embed the
    // proxy. Derived from the same domainName the CORS model is centred on
    // (cors-deployment skill) so the framing allowlist and the CORS
    // allowlist can never drift apart.
    const frameAncestors = buildMcpSandboxFrameAncestors(
      domainName,
      config.mcpSandbox.extraFrameAncestors,
    );

    // ============================================================
    // S3 — holds the static proxy shell (private, OAC-only)
    // ============================================================
    // No public access, no website hosting, no CORS: the shell is loaded
    // only by being framed (an HTML document navigation), never via XHR.
    // Content is fully reproducible from source, so removal policy follows
    // the standard retain/destroy helper like every other bucket.
    this.bucket = new s3.Bucket(this, 'McpSandboxBucket', {
      bucketName: getResourceName(config, 'mcp-sandbox', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    // ============================================================
    // CloudFront — terminates TLS, runs the dynamic-CSP function
    // ============================================================
    // The CSP itself is composed PER-REQUEST by a CloudFront Function on
    // viewer-response (see `assets/mcp-sandbox/csp-function.js`). The
    // function reads the `?csp=` query param the SPA appends when framing
    // proxy.html — the JSON shape matches the spec's `McpUiResourceCsp`
    // (`_meta.ui.csp`) and the function honors declared
    // `connectDomains`/`resourceDomains`/`frameDomains`/`baseUriDomains`.
    // Apps that omit `_meta.ui.csp` fall through to the same reference-
    // matching default the previous static CSP shipped, so the 22/25
    // example servers that worked before continue to work.
    //
    // The ResponseHeadersPolicy is left intentionally CSP-less. Doing
    // both (static via RHP + dynamic via function) would mean every
    // response carries two `Content-Security-Policy` headers; browsers
    // intersect them, which would silently re-deny anything an App
    // legitimately declared. The CFN is the single source of truth for
    // the CSP directive; other security headers (HSTS, Referrer-Policy,
    // X-Content-Type-Options) remain on the RHP since they don't vary
    // per resource.
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      'McpSandboxResponseHeaders',
      {
        responseHeadersPolicyName: getResourceName(config, 'mcp-sandbox-headers'),
        comment: 'Security headers (HSTS, Referrer-Policy, X-Content-Type-Options) for the MCP Apps sandbox proxy. CSP is composed per-request by the dynamic-CSP CloudFront Function.',
        securityHeadersBehavior: {
          contentTypeOptions: { override: true },
          // Intentionally NOT setting frameOptions — `frame-ancestors`
          // in the dynamic CSP is the modern equivalent and the control
          // we care about. Setting X-Frame-Options too would only add a
          // legacy, less expressive duplicate.
          referrerPolicy: {
            referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER,
            override: true,
          },
          strictTransportSecurity: {
            accessControlMaxAge: cdk.Duration.days(365),
            includeSubdomains: true,
            override: true,
          },
        },
      },
    );

    // Dynamic-CSP CloudFront Function. The source ships the FRAME_ANCESTORS
    // placeholder string; we substitute it for the real source list here
    // so unit tests can run the file as-is. JS_2_0 runtime is required —
    // the function uses ES2017 features (regex literals, template
    // strings, JSON.parse) that the legacy JS_1_0 runtime doesn't accept.
    const cspFunctionCode = loadMcpSandboxCspFunctionCode(frameAncestors);
    const cspFunction = new cloudfront.Function(this, 'McpSandboxCspFunction', {
      functionName: getResourceName(config, 'mcp-sandbox-csp'),
      comment: 'Composes the per-resource Content-Security-Policy header from the ?csp= query parameter (matching modelcontextprotocol/ext-apps basic-host/serve.ts).',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(cspFunctionCode),
    });

    // Static shell: a short cache is fine and BucketDeployment invalidates on
    // every deploy so shell changes propagate immediately. No cookies / query
    // / headers participate in the cache key.
    const cachePolicy = new cloudfront.CachePolicy(this, 'McpSandboxCachePolicy', {
      cachePolicyName: getResourceName(config, 'mcp-sandbox-cache'),
      comment: 'Cache policy for the MCP Apps sandbox proxy shell',
      defaultTtl: cdk.Duration.minutes(5),
      minTtl: cdk.Duration.seconds(0),
      maxTtl: cdk.Duration.hours(1),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
      headerBehavior: cloudfront.CacheHeaderBehavior.none(),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.none(),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    const distributionProps: cloudfront.DistributionProps = {
      comment: getResourceName(config, 'mcp-sandbox-cdn'),
      defaultRootObject: 'proxy.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        compress: true,
        // The CSP function runs on viewer-response — including cache hits —
        // so the CSP is composed fresh from the request's `?csp=` query
        // every time. That lets us keep the cache key simple (no `?csp=`
        // included) while still emitting per-resource CSPs: one cached
        // body, dynamic header.
        functionAssociations: [
          {
            function: cspFunction,
            eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE,
          },
        ],
      },
      // Cheapest price class — the shell is tiny and not latency-critical.
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      enabled: true,
      ...(useCustomDomain
        ? {
            domainNames: [proxySubdomain!],
            certificate: acm.Certificate.fromCertificateArn(
              this,
              'McpSandboxCertificate',
              certificateArn!,
            ),
          }
        : {}),
    };

    this.distribution = new cloudfront.Distribution(
      this,
      'McpSandboxDistribution',
      distributionProps,
    );

    // ============================================================
    // Deploy the static shell into the bucket (self-contained)
    // ============================================================
    // Source.asset of plain files needs no Docker — CDK zips locally and the
    // aws-cdk-lib BucketDeployment Lambda uploads it. distribution +
    // distributionPaths wires an automatic CloudFront invalidation so a
    // re-deployed shell is served immediately despite the cache policy.
    new s3deploy.BucketDeployment(this, 'McpSandboxShellDeployment', {
      sources: [
        s3deploy.Source.asset(path.resolve(__dirname, '..', 'assets', 'mcp-sandbox')),
      ],
      destinationBucket: this.bucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      prune: true,
    });

    // ============================================================
    // Route53 — alias the subdomain to CloudFront (custom domain only)
    // ============================================================
    if (useCustomDomain) {
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.infrastructureHostedZoneDomain!,
      });

      new route53.ARecord(this, 'McpSandboxAliasRecord', {
        zone: hostedZone,
        recordName: proxySubdomain!,
        target: route53.RecordTarget.fromAlias(
          new route53Targets.CloudFrontTarget(this.distribution),
        ),
        comment: 'MCP Apps sandbox-proxy origin — proxies to CloudFront → S3 shell',
      });
    }

    // ============================================================
    // SSM export — the outward contract (one-way; nothing reads it yet)
    // ============================================================
    this.proxyOrigin = useCustomDomain
      ? `https://${proxySubdomain}`
      : `https://${this.distribution.distributionDomainName}`;

    new ssm.StringParameter(this, 'McpSandboxOriginParameter', {
      parameterName: `/${config.projectPrefix}/mcp-sandbox/origin`,
      stringValue: this.proxyOrigin,
      description: 'Origin serving the MCP Apps sandbox proxy shell (https://mcp-sandbox.{domain})',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Human-readable CloudFormation outputs for deploy-time visibility.
    new cdk.CfnOutput(this, 'McpSandboxOrigin', {
      value: this.proxyOrigin,
      description: 'MCP Apps sandbox-proxy origin URL',
    });
    new cdk.CfnOutput(this, 'McpSandboxProxyUrl', {
      value: `${this.proxyOrigin}/proxy.html`,
      description: 'Fully-qualified URL of the sandbox proxy shell',
    });
    new cdk.CfnOutput(this, 'McpSandboxDistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront distribution ID for the sandbox-proxy origin',
    });
  }
}
