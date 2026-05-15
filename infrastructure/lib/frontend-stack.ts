import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags, getRemovalPolicy, getAutoDeleteObjects, buildCorsOrigins } from './config';

export interface FrontendStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Frontend Stack - S3 + CloudFront + Optional Route53
 * 
 * This stack creates:
 * - S3 bucket for static website hosting
 * - CloudFront distribution with OAC (Origin Access Control)
 * - Optional Route53 A record for custom domain
 * - SSM parameters for cross-stack references
 */
export class FrontendStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  public readonly distributionDomainName: string;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================================
    // ============================================================================
    // SSM Parameter Imports - Backend URLs for Runtime Configuration
    // ============================================================================
    // These parameters are exported by the backend stacks and must exist before
    // this stack can be deployed. The frontend stack depends on:
    // 1. InfrastructureStack - exports ALB URL to SSM
    //
    // Deployment order: InfrastructureStack → AppApiStack → FrontendStack
    //
    // Note: The frontend only needs the App API URL (ALB). The inference API
    // is accessed through the App API, not directly from the frontend.
    // ============================================================================

    let appApiUrl: string;

    try {
      // Import App API URL from SSM Parameter Store
      // This parameter is created by InfrastructureStack after ALB creation
      // Parameter format: /${projectPrefix}/network/alb-url
      // Example value: https://api.example.com or http://alb-123.us-east-1.elb.amazonaws.com
      appApiUrl = ssm.StringParameter.valueForStringParameter(
        this,
        `/${config.projectPrefix}/network/alb-url`
      );
    } catch (error) {
      throw new Error(
        `Failed to import App API URL from SSM Parameter Store. ` +
        `Ensure InfrastructureStack has been deployed and exports the parameter: ` +
        `/${config.projectPrefix}/network/alb-url. ` +
        `Error: ${error}`
      );
    }

    // ============================================================================
    // Runtime configuration is no longer fetched from S3.
    // ============================================================================
    // Pre-Phase-7 the SPA fetched `/config.json` at startup. After the Phase 7
    // cleanup the only field worth keeping was `appApiUrl`, which is now
    // hardcoded into the bundle via Angular `fileReplacements` (`/api` for
    // production, `http://localhost:8000` for local dev). `version` is baked
    // in by the frontend's `prebuild` script reading the root VERSION file,
    // and `cognitoDomainUrl` moved server-side behind
    // `GET /admin/auth-providers/cognito-redirect-uri` on app-api.
    //
    // The `appApiUrl` SSM token is still resolved below: it feeds the
    // CloudFront `/api/*` HttpOrigin that fronts the same-origin browser
    // path, just no longer the deprecated runtime-config blob.
    console.log('📥 Imported backend URLs from SSM:');
    console.log(`   App API URL: ${appApiUrl}`);

    // Generate bucket name with account ID to ensure global uniqueness
    const bucketName = config.frontend.bucketName || 
                       getResourceName(config, 'frontend', config.awsAccount);

    // Create S3 bucket for static website hosting
    this.bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName,
      // Block all public access - CloudFront will access via OAC
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      // Enable versioning for rollback capability
      versioned: true,
      // Encryption at rest
      encryption: s3.BucketEncryption.S3_MANAGED,
      // Lifecycle policy to clean up old versions
      lifecycleRules: [
        {
          id: 'DeleteOldVersions',
          noncurrentVersionExpiration: cdk.Duration.days(30),
          enabled: true,
        },
      ],
      // Removal policy based on retention configuration
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    // Create Origin Access Control (OAC) for CloudFront
    const _oac = new cloudfront.CfnOriginAccessControl(this, 'FrontendOAC', {
      originAccessControlConfig: {
        name: getResourceName(config, 'frontend-oac'),
        originAccessControlOriginType: 's3',
        signingBehavior: 'always',
        signingProtocol: 'sigv4',
      },
    });

    // CloudFront cache policy for static website
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

    // Response headers policy for security.
    //
    // When artifacts are enabled, allow the SPA to embed iframes from the
    // artifact origin. `frame-src` is the only CSP directive we set —
    // without `default-src`, the browser does not restrict any other
    // resource types, so this addition cannot break existing functionality
    // (scripts, styles, fonts, etc. remain unrestricted by CSP). The other
    // resource types are still defended by the existing X-Content-Type-
    // Options, Referrer-Policy, and HSTS headers below.
    const artifactsOrigin = config.artifacts.enabled && config.domainName
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
            referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
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
      }
    );

    // ============================================================================
    // BFF /api/* behavior — same-origin proxy to the app-api ALB
    // ============================================================================
    // Phase 6a of the BFF Token Handler migration. The SPA hits `/api/*` on the
    // CloudFront origin; CloudFront forwards to the app-api ALB. This makes
    // app-api same-origin with the SPA so `__Host-` session cookies work and
    // there is no CORS preflight on the chat hot path.
    //
    // - HttpOrigin hostname is parsed out of the absolute SSM ALB URL using
    //   Fn.split/Fn.select because the value is a CDK token at synth time.
    // - Path-strip CloudFront Function removes `/api` so app-api stays
    //   prefix-unaware (local dev still hits `localhost:8000` directly).
    // - CACHING_DISABLED + ALL_VIEWER_EXCEPT_HOST_HEADER: every /api/* response
    //   is dynamic and must forward cookies + CSRF + auth headers untouched.
    // - No response-headers-policy: SSE responses must keep the origin's
    //   Content-Type/Cache-Control/X-Accel-Buffering headers verbatim.
    // - compress: false because gzip on text/event-stream re-introduces the
    //   buffering bug we fixed in Phase 4.
    // ============================================================================

    // Strip leading "/api" from the request URI before forwarding to the ALB.
    // CloudFront Functions run at viewer-request, ~1ms, no Lambda cold start.
    const apiPathStripFunction = new cloudfront.Function(this, 'ApiPathStripFunction', {
      functionName: getResourceName(config, 'api-path-strip'),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      comment: 'Strip /api prefix before forwarding requests to the app-api ALB origin',
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
    });

    // Extract the bare hostname from the absolute ALB URL (e.g.
    // "https://api.example.com" -> "api.example.com"). The value is a CDK
    // token resolved at deploy time, so use Fn.split/Fn.select rather than
    // JS string ops.
    const appApiOriginHostname = cdk.Fn.select(2, cdk.Fn.split('/', appApiUrl));

    // HTTPS to the origin when an ALB cert is configured; HTTP fallback for
    // dev environments without `certificateArn`. CloudFront → ALB always
    // crosses public internet, so HTTPS is preferred whenever possible.
    const appApiOrigin = new origins.HttpOrigin(appApiOriginHostname, {
      protocolPolicy: config.certificateArn
        ? cloudfront.OriginProtocolPolicy.HTTPS_ONLY
        : cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      // Long-running SSE streams need a generous read timeout. 60s is the
      // CloudFront default max without a service-quota increase; if any SSE
      // stream goes >60s without sending data, the proxy needs to emit a
      // heartbeat (verified during the Phase 6 soak window).
      readTimeout: cdk.Duration.seconds(60),
      keepaliveTimeout: cdk.Duration.seconds(60),
    });

    const apiBehavior: cloudfront.BehaviorOptions = {
      origin: appApiOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      compress: false,
      functionAssociations: [
        {
          function: apiPathStripFunction,
          eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
        },
      ],
    };

    // ============================================================================
    // SPA routing — rewrite non-asset paths to /index.html at viewer-request
    // ============================================================================
    // Distribution-wide `errorResponses` (the previous approach) caught 403/404
    // from EVERY behavior, including `/api/*`, which silently rewrote real API
    // errors into `200 + index.html` and broke JSON parsing on the SPA. Doing
    // the rewrite at viewer-request on the default behavior keeps SPA fallback
    // scoped to S3-bound traffic and lets API 4xx responses pass through with
    // their original status and JSON body.
    //
    // Anything with a file extension (`.js`, `.css`, `.png`, etc.) is treated
    // as a static asset and forwarded to S3 unchanged. Everything else (SPA
    // routes like `/auth/login`, `/chat/123`) is rewritten to `/index.html`
    // so Angular's router can take over.
    const spaRoutingFunction = new cloudfront.Function(this, 'SpaRoutingFunction', {
      functionName: getResourceName(config, 'spa-routing'),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      comment: 'Rewrite SPA routes to /index.html so Angular can handle client-side routing',
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
    });

    // CloudFront distribution configuration
    let distributionProps: cloudfront.DistributionProps = {
      comment: `${config.projectPrefix} Frontend Distribution`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
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
      priceClass: cloudfront.PriceClass[config.frontend.cloudFrontPriceClass as keyof typeof cloudfront.PriceClass],
      enabled: true,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    };

    // Add custom domain and certificate if configured
    if (config.domainName && config.frontend.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        config.frontend.certificateArn
      );
      distributionProps = {
        ...distributionProps,
        domainNames: [config.domainName],
        certificate: certificate,
      };
    }

    // Create CloudFront distribution
    this.distribution = new cloudfront.Distribution(this, 'FrontendDistribution', distributionProps);

    // Update the S3 bucket policy to allow CloudFront OAC access
    this.bucket.addToResourcePolicy(
      new cdk.aws_iam.PolicyStatement({
        effect: cdk.aws_iam.Effect.ALLOW,
        principals: [new cdk.aws_iam.ServicePrincipal('cloudfront.amazonaws.com')],
        actions: ['s3:GetObject'],
        resources: [this.bucket.arnForObjects('*')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': `arn:aws:cloudfront::${config.awsAccount}:distribution/${this.distribution.distributionId}`,
          },
        },
      })
    );

    // Store distribution domain name
    this.distributionDomainName = this.distribution.distributionDomainName;

    // Create Route53 A record if domain is configured
    if (config.domainName) {
      // Look up the hosted zone
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.domainName,
      });

      // Create A record aliasing to CloudFront
      new route53.ARecord(this, 'FrontendARecord', {
        zone: hostedZone,
        recordName: config.domainName,
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(this.distribution)
        ),
      });
    }

    // Store parameters in SSM Parameter Store for cross-stack references
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

    // Construct CORS origins list via shared helper
    const corsOrigins = buildCorsOrigins(config, config.frontend.additionalCorsOrigins).join(',');

    // Export CORS origins for runtime provisioner (SSM rejects empty string values)
    if (corsOrigins) {
      new ssm.StringParameter(this, 'CorsOriginsParameter', {
        parameterName: `/${config.projectPrefix}/frontend/cors-origins`,
        stringValue: corsOrigins,
        description: 'Comma-separated list of allowed CORS origins for OAuth flows',
        tier: ssm.ParameterTier.STANDARD,
      });
    }

    new ssm.StringParameter(this, 'BucketNameParameter', {
      parameterName: `/${config.projectPrefix}/frontend/bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'S3 bucket name for frontend assets',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================================
    // Update RAG Documents Bucket CORS (if RAG stack is enabled)
    // ============================================================================
    // After CloudFront distribution is created, update the RAG documents bucket
    // CORS configuration to include the frontend URL. This ensures document uploads
    // work from the deployed frontend without manual CORS configuration.
    // ============================================================================

    if (config.ragIngestion.enabled) {
      const ragDocumentsBucketName = ssm.StringParameter.valueForStringParameter(
        this,
        `/${config.projectPrefix}/rag/documents-bucket-name`
      );

      const updateCorsHandler = new lambda.Function(this, 'UpdateRagCorsFn', {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: 'index.handler',
        code: lambda.Code.fromInline(`
import boto3
import cfnresponse

s3 = boto3.client('s3')

def handler(event, context):
    try:
        if event['RequestType'] in ['Create', 'Update']:
            bucket = event['ResourceProperties']['BucketName']
            frontend_url = event['ResourceProperties']['FrontendUrl']
            
            # Get existing CORS rules
            try:
                existing = s3.get_bucket_cors(Bucket=bucket)
                rules = existing.get('CORSRules', [])
            except s3.exceptions.ClientError:
                rules = []
            
            # Find or create the rule for document uploads
            found = False
            for rule in rules:
                if 'PUT' in rule.get('AllowedMethods', []):
                    # Update existing rule to include frontend URL
                    origins = set(rule.get('AllowedOrigins', []))
                    origins.add(frontend_url)
                    origins.add('http://localhost:4200')  # Keep localhost for dev
                    rule['AllowedOrigins'] = list(origins)
                    found = True
                    break
            
            if not found:
                # Create new rule
                rules.append({
                    'AllowedOrigins': [frontend_url, 'http://localhost:4200'],
                    'AllowedMethods': ['GET', 'PUT', 'HEAD'],
                    'AllowedHeaders': ['Content-Type', 'Content-Length', 'x-amz-*'],
                    'ExposeHeaders': ['ETag', 'Content-Length', 'Content-Type'],
                    'MaxAgeSeconds': 3600
                })
            
            # Update bucket CORS
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={'CORSRules': rules})
            print(f'Updated CORS for {bucket} to include {frontend_url}')
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        print(f'Error: {str(e)}')
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
`),
        timeout: cdk.Duration.minutes(2),
      });

      // Grant permission to update the RAG documents bucket CORS
      updateCorsHandler.addToRolePolicy(new iam.PolicyStatement({
        actions: ['s3:GetBucketCors', 's3:PutBucketCors'],
        resources: [`arn:aws:s3:::${ragDocumentsBucketName}`],
      }));

      // Create custom resource to trigger the update
      const frontendUrl = config.domainName 
        ? `https://${config.domainName}`
        : `https://${this.distributionDomainName}`;
      
      new cdk.CustomResource(this, 'UpdateRagCors', {
        serviceToken: updateCorsHandler.functionArn,
        properties: {
          BucketName: ragDocumentsBucketName,
          FrontendUrl: frontendUrl,
          // Force update on every deployment by including timestamp
          Timestamp: Date.now().toString(),
        },
      });

      console.log('📝 RAG documents bucket CORS will be updated with frontend URL');
    }

    // CloudFormation Outputs
    new cdk.CfnOutput(this, 'FrontendBucketName', {
      value: this.bucket.bucketName,
      description: 'S3 Bucket Name',
      exportName: `${config.projectPrefix}-FrontendBucketName`,
    });

    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront Distribution ID',
      exportName: `${config.projectPrefix}-DistributionId`,
    });

    new cdk.CfnOutput(this, 'DistributionDomainName', {
      value: this.distributionDomainName,
      description: 'CloudFront Distribution Domain Name',
      exportName: `${config.projectPrefix}-DistributionDomainName`,
    });

    new cdk.CfnOutput(this, 'WebsiteUrl', {
      value: config.domainName || `https://${this.distributionDomainName}`,
      description: 'Frontend Website URL',
      exportName: `${config.projectPrefix}-WebsiteUrl`,
    });
  }
}
