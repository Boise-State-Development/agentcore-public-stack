import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as path from 'path';
import {
  AppConfig,
  applyStandardTags,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from './config';

export interface ArtifactsStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Artifacts Stack — iframe-isolated artifact rendering pipeline.
 *
 * Provisions everything required to serve user-generated artifacts
 * (HTML, code, markdown, SVG) into a sandboxed cross-origin iframe:
 *
 *   - DynamoDB `user-artifacts` table (heads + version log, GSI by session)
 *   - S3 `artifacts-content` bucket (private, no CORS — HTML served via CF)
 *   - Render Lambda (validates render-token JWT, fetches blob, returns
 *     HTML with strict CSP)
 *   - CloudFront distribution for `artifacts.{domainName}` (terminates TLS,
 *     attaches the security headers policy)
 *   - Route53 A record aliasing the subdomain to CloudFront
 *
 * Cross-stack contract (SSM, all `/{projectPrefix}/artifacts/*`):
 *
 *   Consumes (published by InfrastructureStack):
 *     /artifacts/render-token-key-arn   Secrets Manager ARN of HMAC key
 *
 *   Publishes (consumed by inference-api, app-api, frontend):
 *     /artifacts/bucket-name            S3 bucket name
 *     /artifacts/bucket-arn             S3 bucket ARN
 *     /artifacts/table-name             DDB table name
 *     /artifacts/table-arn              DDB table ARN
 *     /artifacts/origin                 "https://artifacts.{domainName}"
 *
 * Dependency direction is one-way: ArtifactsStack reads InfrastructureStack
 * via SSM and publishes its own SSM parameters. Consumers (inference-api,
 * app-api, frontend) read those parameters. No consumer publishes anything
 * that ArtifactsStack reads — this is what prevents CDK circular references.
 *
 * Deploy order: InfrastructureStack → ArtifactsStack → (inference-api,
 * app-api, frontend). Parallel-safe with RagIngestionStack and
 * SageMakerFineTuningStack which neither read nor write artifact SSM keys.
 */
export class ArtifactsStack extends cdk.Stack {
  public readonly artifactsTable: dynamodb.Table;
  public readonly artifactsBucket: s3.Bucket;
  public readonly renderFunction: lambda.Function;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: ArtifactsStackProps) {
    super(scope, id, props);

    const { config } = props;
    applyStandardTags(this, config);

    // Validation in config.ts has already enforced these for enabled stacks.
    // Non-null assertions are safe here.
    const domainName = config.domainName!;
    const hostedZoneDomain = config.infrastructureHostedZoneDomain!;
    const certificateArn = config.artifacts.certificateArn!;
    const artifactsSubdomain = `artifacts.${domainName}`;

    // CSP frame-ancestors source list: the deployed SPA origin, plus any
    // extra origins (e.g. http://localhost:4200 for a local SPA pointed at
    // this env). Space-separated per the CSP grammar — consumed identically
    // by the CloudFront response-headers-policy and the render Lambda's own
    // defense-in-depth CSP.
    const frameAncestors = [
      `https://${domainName}`,
      ...config.artifacts.extraFrameAncestors,
    ].join(' ');

    // ============================================================
    // DynamoDB — artifact metadata + per-version log
    // ============================================================
    // PK: USER#{user_id}
    // SK: ARTIFACT#{artifact_id}#HEAD            (current state, 1 per artifact)
    // SK: ARTIFACT#{artifact_id}#V#{version:05d} (immutable version records)
    //
    // GSI SessionIndex:
    //   PK: SESSION#{session_id}
    //   SK: ARTIFACT#{updated_at}#{artifact_id}
    // ...lets the SPA list artifacts for the current session newest-first.
    this.artifactsTable = new dynamodb.Table(this, 'ArtifactsTable', {
      tableName: getResourceName(config, 'user-artifacts'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: config.production,
      },
      timeToLiveAttribute: 'ttl',
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: getRemovalPolicy(config),
    });

    this.artifactsTable.addGlobalSecondaryIndex({
      indexName: 'SessionIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ============================================================
    // S3 — artifact content blobs
    // ============================================================
    // Layout: {user_id}/{artifact_id}/v{n}/index.html (+ sibling assets)
    // Private, no CORS — the iframe loads HTML directly from CloudFront
    // (which proxies to the render Lambda), never via XHR. Versioning is
    // at the DDB layer (immutable per-version rows + content pointer),
    // not S3 — keeps the S3 object lifecycle simple and predictable.
    this.artifactsBucket = new s3.Bucket(this, 'ArtifactsContentBucket', {
      bucketName: getResourceName(config, 'artifacts-content'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          // Clean up failed multipart uploads (mostly large React bundles)
          // so they don't accumulate storage charges.
          id: 'abort-stale-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
        {
          // Soft-deleted artifacts: the backend tags objects with
          // `lifecycle-class=deleted` on artifact delete, and this rule
          // reaps them after the configured retention window. Keeps the
          // "undelete" undo affordance feasible without unbounded storage.
          id: 'expire-soft-deleted',
          tagFilters: { 'lifecycle-class': 'deleted' },
          expiration: cdk.Duration.days(config.artifacts.retentionDays),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    // ============================================================
    // Render Lambda — validates JWT, fetches blob, wraps in HTML+CSP
    // ============================================================
    // ARM64 for cost; Python to match the rest of the backend toolchain.
    // The function ships with NO third-party deps in this scaffold —
    // when JWT verification + S3 read are implemented, add a
    // `requirements.txt` next to handler.py and switch to
    // `lambda.Code.fromAsset` with a Python bundling option, or move to
    // a `DockerImageFunction` (matches the rag-ingestion stack pattern).
    const renderTokenKeyArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/artifacts/render-token-key-arn`,
    );

    this.renderFunction = new lambda.Function(this, 'RenderFunction', {
      functionName: getResourceName(config, 'artifact-render'),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(
        path.resolve(__dirname, '..', '..', 'backend', 'src', 'lambdas', 'artifact_render'),
      ),
      memorySize: 512,
      timeout: cdk.Duration.seconds(5),
      environment: {
        ARTIFACTS_BUCKET: this.artifactsBucket.bucketName,
        ARTIFACTS_TABLE: this.artifactsTable.tableName,
        RENDER_TOKEN_SECRET_ARN: renderTokenKeyArn,
        FRAME_ANCESTOR_ORIGIN: frameAncestors,
        // Pinned CSP allow-list. Adjust here if/when the artifact runtime
        // grows new permitted external script origins. Keep in exact sync
        // with the `script-src` line in `cspDirectives` below — the render
        // Lambda reads this env var, CloudFront stamps the literal, and the
        // two must be identical (defense in depth).
        CSP_SCRIPT_SRC:
          "'self' 'unsafe-inline' https://cdn.tailwindcss.com https://esm.sh https://cdn.jsdelivr.net https://unpkg.com",
      },
    });

    // Read access to artifact content + DDB metadata. No write access —
    // writes flow from inference-api's agent tool, granted in InferenceApiStack.
    this.artifactsBucket.grantRead(this.renderFunction);
    this.artifactsTable.grantReadData(this.renderFunction);

    // Read the HMAC signing key from Secrets Manager. Include the wildcard
    // suffix so the policy matches the random-suffix actual ARN.
    this.renderFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ReadRenderTokenSecret',
        actions: ['secretsmanager:GetSecretValue'],
        resources: [`${renderTokenKeyArn}*`],
      }),
    );

    // Lambda Function URL — invoked by CloudFront only.
    //
    // AWS_IAM auth + Origin Access Control (OAC) below: CloudFront signs
    // each origin request with SigV4 using a service-principal trust the
    // Lambda accepts, and the Function URL refuses unsigned requests. This
    // blocks direct invocation at the lambdaUrl.amazonaws.com hostname —
    // no application-layer host check needed. (Earlier draft used NONE +
    // CloudFront-as-gatekeeper, but `FunctionUrlOrigin.withOriginAccessControl`
    // requires AWS_IAM; CDK enforces this at synth time.)
    const functionUrl = this.renderFunction.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
    });

    // ============================================================
    // CloudFront — terminates TLS, attaches CSP, caches nothing
    // ============================================================
    const certificate = acm.Certificate.fromCertificateArn(
      this,
      'ArtifactsCertificate',
      certificateArn,
    );

    // Strict CSP for the artifact origin. `connect-src 'none'` is the
    // critical line — artifact JS cannot fetch the app API, cannot phone
    // home, cannot exfiltrate. `frame-ancestors` pins the parent origin
    // so other sites can't embed your users' artifacts.
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
          // NOT setting frameOptions here — `frame-ancestors` above is the
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

    // FunctionUrlOrigin proxies to the Lambda Function URL. Caching is
    // disabled because each render-token JWT is per-version-per-session
    // and tokens carry their own auth — no useful cache key exists.
    this.distribution = new cloudfront.Distribution(this, 'ArtifactsDistribution', {
      comment: getResourceName(config, 'artifacts-cdn'),
      domainNames: [artifactsSubdomain],
      certificate,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: origins.FunctionUrlOrigin.withOriginAccessControl(functionUrl),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        compress: true,
      },
      // Reuse the cheapest price class for artifacts — these aren't
      // latency-critical and most of the audience is regional anyway.
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    // ============================================================
    // Route53 — alias artifacts.{domainName} to the CloudFront distro
    // ============================================================
    const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
      domainName: hostedZoneDomain,
    });

    new route53.ARecord(this, 'ArtifactsAliasRecord', {
      zone: hostedZone,
      recordName: artifactsSubdomain,
      target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(this.distribution)),
      comment: 'Artifact iframe origin — proxies to CloudFront → render Lambda',
    });

    // ============================================================
    // SSM exports — the outward contract
    // ============================================================
    new ssm.StringParameter(this, 'ArtifactsBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/bucket-name`,
      stringValue: this.artifactsBucket.bucketName,
      description: 'S3 bucket holding artifact content blobs',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ArtifactsBucketArnParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/bucket-arn`,
      stringValue: this.artifactsBucket.bucketArn,
      description: 'ARN of the artifact content bucket (for IAM grants)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ArtifactsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/table-name`,
      stringValue: this.artifactsTable.tableName,
      description: 'DynamoDB table holding artifact heads + version log',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ArtifactsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/table-arn`,
      stringValue: this.artifactsTable.tableArn,
      description: 'ARN of the artifacts table (for IAM grants)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ArtifactsOriginParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/origin`,
      stringValue: `https://${artifactsSubdomain}`,
      description: 'Origin where artifact iframes are served (https://artifacts.{domain})',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Human-readable CloudFormation outputs for deploy-time visibility.
    new cdk.CfnOutput(this, 'ArtifactsOrigin', {
      value: `https://${artifactsSubdomain}`,
      description: 'Artifact iframe origin URL',
    });
    new cdk.CfnOutput(this, 'ArtifactsDistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront distribution ID for the artifact origin',
    });
  }
}
