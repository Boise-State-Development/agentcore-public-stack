import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface ArtifactsDistributionConstructProps {
  config: AppConfig;
  /** Render Lambda's Function URL (proxied by CloudFront via OAC). */
  renderFunctionUrl: lambda.IFunctionUrl;
  /** CSP `frame-ancestors` source list (space-separated). */
  frameAncestors: string;
}

/**
 * ArtifactsDistributionConstruct — CloudFront for the artifact iframe
 * origin, with a Route53 ALIAS A record on `artifacts.{domainName}`.
 *
 * Fronts the artifact render Lambda with TLS termination and a strict
 * CSP. `connect-src 'none'` is the critical line — artifact JS cannot
 * fetch the app API, cannot phone home, cannot exfiltrate.
 * `frame-ancestors` pins the parent SPA origin so other sites cannot
 * embed users' artifacts.
 *
 * Caching is disabled because each render-token JWT is per-version-
 * per-session and tokens carry their own auth — no useful cache key
 * exists.
 *
 * Cost-optimised price class (PRICE_CLASS_100) — artifacts aren't
 * latency-critical and most of the audience is regional.
 *
 * Custom domain + cert + Route53 are attached only when BOTH a domain
 * and an ACM cert are configured (config.ts enforces both — and the
 * constructor guards the domain-without-cert case). Keeping it
 * conditional lets the construct synthesize on the CloudFront default
 * domain for unit/synth tests and domain-less local stacks, mirroring
 * McpSandboxDistributionConstruct.
 *
 * SSM publication: `/{prefix}/artifacts/origin` →
 * `https://artifacts.{domainName}` (or the CloudFront default domain
 * fallback when no custom domain is configured; consumed by
 * inference-api, app-api, frontend).
 */
export class ArtifactsDistributionConstruct extends Construct {
  public readonly distribution: cloudfront.Distribution;
  /**
   * Full URL of the artifacts iframe origin (https://artifacts.{domain}).
   * Exposed so other compute constructs (notably the App API)
   * can wire it via direct construct refs instead of round-tripping
   * through SSM, which would chicken-and-egg on a same-stack first
   * deploy.
   */
  public readonly originUrl: string;

  constructor(
    scope: Construct,
    id: string,
    props: ArtifactsDistributionConstructProps,
  ) {
    super(scope, id);

    const { config, renderFunctionUrl, frameAncestors } = props;

    // Fail loudly on the dangerous middle case: a real domain is configured
    // but no ACM cert is available for the artifacts origin. Without this the
    // custom-domain branch below would hand `undefined` to `fromCertificateArn`,
    // producing an opaque CDK error (`Cannot read properties of undefined
    // (reading 'startsWith')`) instead of an actionable one. Mirrors the
    // McpSandboxDistributionConstruct guard. The cert is resolved in config.ts,
    // where each CloudFront section falls back to the shared
    // CDK_CLOUDFRONT_CERTIFICATE_ARN when its own ARN is unset — so this only
    // trips when neither the section-specific nor the shared cert was supplied
    // for a domained deploy.
    const domainName = config.domainName;
    const certificateArn = config.artifacts.certificateArn;
    if (domainName && !certificateArn) {
      const artifactsSubdomainForError = `artifacts.${domainName}`;
      throw new Error(
        `Artifacts iframe origin requires an ACM certificate when a domain is configured. ` +
          `domainName="${domainName}" is set but config.artifacts.certificateArn is empty. ` +
          `Set CDK_ARTIFACTS_CERTIFICATE_ARN (or the shared CDK_CLOUDFRONT_CERTIFICATE_ARN) ` +
          `to a us-east-1 cert covering ${artifactsSubdomainForError}. Without it the ` +
          `artifact iframe origin has no valid TLS cert and the SPA cannot frame artifacts.`,
      );
    }

    // Custom domain + cert + Route53 are attached only when BOTH a domain and
    // a cert are configured (the guard above rejects the domain-without-cert
    // case). Keeping it conditional lets the construct synthesize on the
    // CloudFront default domain for unit/synth tests and domain-less local
    // stacks — matching McpSandboxDistributionConstruct, instead of crashing
    // on `fromCertificateArn(undefined)`.
    const useCustomDomain = Boolean(domainName && certificateArn);
    const artifactsSubdomain = domainName
      ? `artifacts.${domainName}`
      : undefined;
    const cspDirectives = [
      `default-src 'none'`,
      `script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://esm.sh https://cdn.jsdelivr.net https://unpkg.com`,
      `style-src 'self' 'unsafe-inline'`,
      `img-src 'self' data: https:`,
      `font-src 'self' data:`,
      `connect-src 'none'`,
      `frame-ancestors ${frameAncestors}`,
      `form-action 'none'`,
      `base-uri 'none'`,
    ].join('; ');

    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      'ArtifactsResponseHeaders',
      {
        responseHeadersPolicyName: getResourceName(config, 'artifacts-headers'),
        comment: 'Strict CSP + security headers for artifact iframe origin',
        securityHeadersBehavior: {
          contentSecurityPolicy: {
            contentSecurityPolicy: cspDirectives,
            override: true,
          },
          contentTypeOptions: { override: true },
          // NOT setting frameOptions — frame-ancestors above is the
          // CSP-native equivalent and is what gets enforced cross-browser.
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

    this.distribution = new cloudfront.Distribution(
      this,
      'ArtifactsDistribution',
      {
        comment: getResourceName(config, 'artifacts-cdn'),
        minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        defaultBehavior: {
          origin: origins.FunctionUrlOrigin.withOriginAccessControl(
            renderFunctionUrl,
          ),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy:
            cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
          compress: true,
        },
        priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
        ...(useCustomDomain
          ? {
              domainNames: [artifactsSubdomain!],
              certificate: acm.Certificate.fromCertificateArn(
                this,
                'ArtifactsCertificate',
                certificateArn!,
              ),
            }
          : {}),
      },
    );

    if (useCustomDomain) {
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.infrastructureHostedZoneDomain!,
      });

      new route53.ARecord(this, 'ArtifactsAliasRecord', {
        zone: hostedZone,
        recordName: artifactsSubdomain!,
        target: route53.RecordTarget.fromAlias(
          new route53Targets.CloudFrontTarget(this.distribution),
        ),
        comment:
          'Artifact iframe origin — proxies to CloudFront → render Lambda',
      });
    }

    this.originUrl = useCustomDomain
      ? `https://${artifactsSubdomain}`
      : `https://${this.distribution.distributionDomainName}`;

  }
}
