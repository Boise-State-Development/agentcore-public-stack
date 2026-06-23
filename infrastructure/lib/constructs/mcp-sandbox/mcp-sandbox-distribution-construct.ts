import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/**
 * Build the CSP `frame-ancestors` source list for the proxy origin.
 *
 * The proxy may ONLY be embedded by the SPA (`ai.client`) origin —
 * `https://{domainName}` plus any explicitly-allowed extras (e.g.
 * `http://localhost:4200` for a local SPA pointed at this env).
 *
 * Falls back to `'none'` (deny all framing) when there is no SPA
 * origin to permit — keeps the construct synthesizable for unit/synth
 * tests and domain-less local stacks without ever silently allowing `*`.
 *
 * Exported so the value is unit-testable directly and there's a single
 * source of truth.
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
 * The full JS string literal in `assets/mcp-sandbox/csp-function.js` that
 * we substitute at synth time. Matching the *quoted* literal (not the
 * inner identifier) lets us replace it with `JSON.stringify(value)`,
 * which handles quote-escaping correctly for any `frame-ancestors` source
 * list — including `'none'` (which would otherwise produce `''none''`,
 * a JS syntax error).
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
    '..',
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

/**
 * The subdomain label for the MCP Apps sandbox-proxy origin.
 *
 * Matches the working name in
 * `docs/kaizen/scoping/mcp-apps-host-renderer.md` and parallels the
 * existing sibling iframe origin `artifacts.{domain}`. Single source of
 * truth for the label — must stay in sync with the
 * `CDK_MCP_SANDBOX_*` workflow env vars.
 */
export const MCP_SANDBOX_SUBDOMAIN_LABEL = 'mcp-sandbox';

export interface McpSandboxDistributionConstructProps {
  config: AppConfig;
  /** Bucket holding `proxy.html` + `proxy.js` (default origin). */
  bucket: s3.IBucket;
}

/**
 * McpSandboxDistributionConstruct — CloudFront + Route53 ALIAS for the
 * MCP Apps sandbox-proxy origin.
 *
 * Terminates TLS, stamps the CSP (frame-ancestors locked to the SPA
 * origin), and proxies to the S3-hosted shell via OAC.
 *
 * Custom domain + cert + Route53 are attached only when BOTH a domain
 * and an ACM cert are configured (config.ts enforces both, plus the
 * hosted zone, whenever the stack is enabled). Keeping it conditional
 * lets the construct still synthesize on the CloudFront default domain
 * for unit/synth tests and domain-less local stacks.
 *
 * Origin exposure: the resolved origin (`https://mcp-sandbox.{domainName}`,
 * or the CloudFront default domain when no custom domain is configured) is
 * surfaced as `proxyOrigin` and threaded through `PlatformComputeRefs`
 * directly into inference-api's `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` env var.
 * It is no longer published to SSM (pre-#396 the standalone stack did).
 */
export class McpSandboxDistributionConstruct extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly proxyOrigin: string;

  constructor(
    scope: Construct,
    id: string,
    props: McpSandboxDistributionConstructProps,
  ) {
    super(scope, id);

    const { config, bucket } = props;

    const domainName = config.domainName;
    const certificateArn = config.mcpSandbox.certificateArn;
    const proxySubdomain = domainName
      ? `${MCP_SANDBOX_SUBDOMAIN_LABEL}.${domainName}`
      : undefined;

    // Fail loudly on the dangerous middle case: a real domain is configured
    // but no cert. Without this the construct would silently fall back to the
    // CloudFront default domain and create NO Route53 ALIAS — so the SPA
    // frames `https://${proxySubdomain}`, which doesn't resolve (NXDOMAIN),
    // and every MCP App fails to load. That is exactly the regression caused
    // when the `CDK_MCP_SANDBOX_CERTIFICATE_ARN` deploy var was dropped from
    // platform.yml. Mirrors the artifacts construct, which requires its cert
    // whenever a domain is deployed. A domain-less stack (synth/unit tests,
    // domain-less local) still falls back cleanly to the CloudFront default.
    if (domainName && !certificateArn) {
      throw new Error(
        `MCP sandbox proxy requires an ACM certificate when a domain is configured. ` +
          `domainName="${domainName}" is set but config.mcpSandbox.certificateArn is empty. ` +
          `Set CDK_MCP_SANDBOX_CERTIFICATE_ARN to a us-east-1 cert covering ${proxySubdomain}. ` +
          `Without it the proxy deploys on the CloudFront default domain with no Route53 ` +
          `record, and the SPA cannot frame MCP Apps.`,
      );
    }

    const useCustomDomain = Boolean(domainName && certificateArn);

    const frameAncestors = buildMcpSandboxFrameAncestors(
      domainName,
      config.mcpSandbox.extraFrameAncestors,
    );

    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      'McpSandboxResponseHeaders',
      {
        responseHeadersPolicyName: getResourceName(
          config,
          'mcp-sandbox-headers',
        ),
        comment:
          'HSTS + Referrer-Policy + X-Content-Type-Options for MCP Apps sandbox proxy. CSP via dynamic CloudFront Function.',
        securityHeadersBehavior: {
          contentTypeOptions: { override: true },
          // NOT setting frameOptions — frame-ancestors in the dynamic CSP
          // is the modern equivalent and the control we care about.
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

    // Dynamic-CSP CloudFront Function. Composes per-resource CSP from
    // the `?csp=` query param the SPA appends when framing proxy.html.
    const cspFunctionCode = loadMcpSandboxCspFunctionCode(frameAncestors);
    const cspFunction = new cloudfront.Function(this, 'McpSandboxCspFunction', {
      functionName: getResourceName(config, 'mcp-sandbox-csp'),
      comment: 'Composes per-resource CSP header from ?csp= query (mirrors ext-apps basic-host/serve.ts).',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(cspFunctionCode),
    });

    const cachePolicy = new cloudfront.CachePolicy(
      this,
      'McpSandboxCachePolicy',
      {
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
      },
    );

    const distributionProps: cloudfront.DistributionProps = {
      comment: getResourceName(config, 'mcp-sandbox-cdn'),
      defaultRootObject: 'proxy.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        compress: true,
        functionAssociations: [
          {
            function: cspFunction,
            eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE,
          },
        ],
      },
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
        comment:
          'MCP Apps sandbox-proxy origin — proxies to CloudFront → S3 shell',
      });
    }

    this.proxyOrigin = useCustomDomain
      ? `https://${proxySubdomain}`
      : `https://${this.distribution.distributionDomainName}`;

  }
}
