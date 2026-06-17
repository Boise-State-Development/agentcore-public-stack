import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  buildCorsOrigins,
  getResourceName,
} from '../../config';

export interface SpaDistributionConstructProps {
  config: AppConfig;
  /** SPA static-asset bucket (default origin). */
  bucket: s3.IBucket;
  /**
   * Absolute ALB URL (with protocol) for the `/api/*` behavior origin.
   * Resolved from SSM by the parent stack — this construct only needs
   * the resolved string.
   */
  appApiUrl: string;
}

/**
 * SpaDistributionConstruct — CloudFront distribution serving the SPA.
 *
 * Behaviors:
 *   - default behavior — S3 origin via OAC, viewer-request CloudFront
 *     Function rewrites SPA routes to `/index.html` so Angular's
 *     client-side router can resolve them. Static assets (anything
 *     with a file extension) pass through unchanged.
 *   - `/api/*` behavior — same-origin proxy to the app-api ALB. A
 *     CloudFront Function strips the `/api` prefix at viewer-request
 *     so app-api stays prefix-unaware. CACHING_DISABLED +
 *     ALL_VIEWER_EXCEPT_HOST_HEADER pass cookies + CSRF + auth headers
 *     untouched. compress=false to preserve `text/event-stream`.
 *
 * Security headers:
 *   - X-Content-Type-Options, X-Frame-Options=DENY (default-deny iframe
 *     embedding), Referrer-Policy=strict-origin-when-cross-origin, HSTS
 *     1y w/ subdomains, X-XSS-Protection.
 *   - The `frame-src` CSP directive opens `https://artifacts.{domainName}`
 *     so the SPA can embed artifact iframes. Other resource types remain unrestricted
 *     by CSP (defended by the other security headers).
 *
 * Custom domain: if `config.domainName` and
 * `config.frontend.certificateArn` are both set, the distribution
 * serves at `config.domainName` and a Route53 ALIAS A record is
 * created in the `config.domainName` hosted zone (looked up at synth).
 *
 * SSM publications:
 *   /{prefix}/frontend/distribution-id
 *   /{prefix}/frontend/url
 *   /{prefix}/frontend/cors-origins        (only when corsOrigins is non-empty)
 */
export class SpaDistributionConstruct extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly distributionDomainName: string;

  constructor(
    scope: Construct,
    id: string,
    props: SpaDistributionConstructProps,
  ) {
    super(scope, id);

    const { config, bucket, appApiUrl } = props;

    // OAC for CloudFront → S3 access (S3 bucket has block-public-access).
    new cloudfront.CfnOriginAccessControl(this, 'FrontendOAC', {
      originAccessControlConfig: {
        name: getResourceName(config, 'frontend-oac'),
        originAccessControlOriginType: 's3',
        signingBehavior: 'always',
        signingProtocol: 'sigv4',
      },
    });

    const cachePolicy = new cloudfront.CachePolicy(this, 'FrontendCachePolicy', {
      cachePolicyName: getResourceName(config, 'frontend-cache'),
      comment: 'Cache policy for frontend static assets',
      defaultTtl: cdk.Duration.hours(24),
      minTtl: cdk.Duration.minutes(1),
      maxTtl: cdk.Duration.days(365),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
      headerBehavior: cloudfront.CacheHeaderBehavior.none(),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.none(),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    const artifactsOrigin = config.domainName
      ? `https://artifacts.${config.domainName}`
      : undefined;

    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      'FrontendResponseHeadersPolicy',
      {
        responseHeadersPolicyName: getResourceName(config, 'frontend-headers'),
        comment: 'Security headers for frontend',
        securityHeadersBehavior: {
          contentTypeOptions: { override: true },
          frameOptions: {
            frameOption: cloudfront.HeadersFrameOption.DENY,
            override: true,
          },
          referrerPolicy: {
            referrerPolicy:
              cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
            override: true,
          },
          strictTransportSecurity: {
            accessControlMaxAge: cdk.Duration.seconds(31536000),
            includeSubdomains: true,
            override: true,
          },
          xssProtection: {
            protection: true,
            modeBlock: true,
            override: true,
          },
          ...(artifactsOrigin
            ? {
                contentSecurityPolicy: {
                  contentSecurityPolicy: `frame-src 'self' ${artifactsOrigin}`,
                  override: true,
                },
              }
            : {}),
        },
      },
    );

    // Path-strip Function for /api/* — runs at viewer-request, ~1ms.
    const apiPathStripFunction = new cloudfront.Function(
      this,
      'ApiPathStripFunction',
      {
        functionName: getResourceName(config, 'api-path-strip'),
        runtime: cloudfront.FunctionRuntime.JS_2_0,
        comment:
          'Strip /api prefix before forwarding requests to the app-api ALB origin',
        code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  if (req.uri === '/api') {
    req.uri = '/';
  } else if (req.uri.indexOf('/api/') === 0) {
    req.uri = req.uri.substring(4);
  }
  return req;
}
`),
      },
    );

    // Extract the bare hostname from the absolute ALB URL token.
    const appApiOriginHostname = cdk.Fn.select(
      2,
      cdk.Fn.split('/', appApiUrl),
    );

    const appApiOrigin = new origins.HttpOrigin(appApiOriginHostname, {
      protocolPolicy: config.certificateArn
        ? cloudfront.OriginProtocolPolicy.HTTPS_ONLY
        : cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      readTimeout: cdk.Duration.seconds(60),
      keepaliveTimeout: cdk.Duration.seconds(60),
    });

    const apiBehavior: cloudfront.BehaviorOptions = {
      origin: appApiOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy:
        cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      compress: false,
      functionAssociations: [
        {
          function: apiPathStripFunction,
          eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
        },
      ],
    };

    // SPA routing function — rewrite non-asset paths to /index.html.
    const spaRoutingFunction = new cloudfront.Function(
      this,
      'SpaRoutingFunction',
      {
        functionName: getResourceName(config, 'spa-routing'),
        runtime: cloudfront.FunctionRuntime.JS_2_0,
        comment:
          'Rewrite SPA routes to /index.html so Angular can handle client-side routing',
        code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  var uri = req.uri;
  // Static asset (has a file extension in the last path segment) → leave as-is.
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  if (lastSegment.indexOf('.') !== -1) {
    return req;
  }
  // SPA route → serve index.html so the Angular router can resolve it.
  req.uri = '/index.html';
  return req;
}
`),
      },
    );

    let distributionProps: cloudfront.DistributionProps = {
      comment: `${config.projectPrefix} Frontend Distribution`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
        functionAssociations: [
          {
            function: spaRoutingFunction,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      additionalBehaviors: {
        '/api/*': apiBehavior,
      },
      defaultRootObject: 'index.html',
      priceClass:
        cloudfront.PriceClass[
          config.frontend.cloudFrontPriceClass as keyof typeof cloudfront.PriceClass
        ],
      enabled: true,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    };

    if (config.domainName && config.frontend.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        config.frontend.certificateArn,
      );
      distributionProps = {
        ...distributionProps,
        domainNames: [config.domainName],
        certificate,
        // Pin to the 2021-vintage minimum: TLS 1.2+ only, CBC ciphers
        // pruned. CloudFront's default (`TLSv1`) accepts TLS 1.0 with
        // legacy ciphers, which is exactly what TLS-baseline scanners
        // flag.
        minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      };
    }

    this.distribution = new cloudfront.Distribution(
      this,
      'FrontendDistribution',
      distributionProps,
    );

    // Update the S3 bucket policy to allow CloudFront OAC access.
    bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
        actions: ['s3:GetObject'],
        resources: [bucket.arnForObjects('*')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': `arn:aws:cloudfront::${config.awsAccount}:distribution/${this.distribution.distributionId}`,
          },
        },
      }),
    );

    this.distributionDomainName = this.distribution.distributionDomainName;

    // Optional Route53 ALIAS for the apex (or sub) custom domain.
    if (config.domainName) {
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.domainName,
      });

      new route53.ARecord(this, 'FrontendARecord', {
        zone: hostedZone,
        recordName: config.domainName,
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(this.distribution),
        ),
      });
    }

    // SSM publications
    new ssm.StringParameter(this, 'DistributionIdParameter', {
      parameterName: `/${config.projectPrefix}/frontend/distribution-id`,
      stringValue: this.distribution.distributionId,
      description: 'CloudFront Distribution ID for frontend',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'FrontendUrlParameter', {
      parameterName: `/${config.projectPrefix}/frontend/url`,
      stringValue: config.domainName || `https://${this.distributionDomainName}`,
      description: 'Frontend website URL',
      tier: ssm.ParameterTier.STANDARD,
    });

    const corsOrigins = buildCorsOrigins(
      config,
      config.frontend.additionalCorsOrigins,
    ).join(',');

    if (corsOrigins) {
    }
  }
}
