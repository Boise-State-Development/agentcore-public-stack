import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

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
      // Removal policy - be careful with DESTROY in production
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
    });

    // Create Origin Access Control (OAC) for CloudFront
    const oac = new cloudfront.CfnOriginAccessControl(this, 'FrontendOAC', {
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

    // Response headers policy for security
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
        },
      }
    );

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
      },
      defaultRootObject: 'index.html',
      // Custom error responses for SPA routing
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.minutes(5),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.minutes(5),
        },
      ],
      priceClass: cloudfront.PriceClass[config.frontend.cloudFrontPriceClass as keyof typeof cloudfront.PriceClass],
      enabled: true,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    };

    // Add custom domain and certificate if configured
    if (config.frontend.domainName && config.frontend.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        config.frontend.certificateArn
      );
      distributionProps = {
        ...distributionProps,
        domainNames: [config.frontend.domainName],
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

    // Create Route53 A record if domain is configured and Route53 is enabled
    if (config.frontend.enableRoute53 && config.frontend.domainName) {
      // Look up the hosted zone
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.frontend.domainName,
      });

      // Create A record aliasing to CloudFront
      new route53.ARecord(this, 'FrontendARecord', {
        zone: hostedZone,
        recordName: config.frontend.domainName,
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
      stringValue: config.frontend.domainName || `https://${this.distributionDomainName}`,
      description: 'Frontend website URL',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'BucketNameParameter', {
      parameterName: `/${config.projectPrefix}/frontend/bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'S3 bucket name for frontend assets',
      tier: ssm.ParameterTier.STANDARD,
    });

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
      value: config.frontend.domainName || `https://${this.distributionDomainName}`,
      description: 'Frontend Website URL',
      exportName: `${config.projectPrefix}-WebsiteUrl`,
    });
  }
}
