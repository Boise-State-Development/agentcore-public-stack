import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as path from 'path';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface McpSandboxBucketConstructProps {
  config: AppConfig;
}

/**
 * McpSandboxBucketConstruct — private S3 bucket for the MCP Apps
 * sandbox-proxy shell.
 *
 * Holds `proxy.html` and `proxy.js` from
 * `infrastructure/assets/mcp-sandbox/`. No public access, no website
 * hosting, no CORS — the shell is loaded only by being framed (an
 * HTML document navigation), never via XHR.
 *
 * Usage:
 *
 *   const bucket = new McpSandboxBucketConstruct(this, 'Bucket', { config });
 *   const dist   = new McpSandboxDistributionConstruct(this, 'Dist', {
 *     config, bucket: bucket.bucket,
 *   });
 *   bucket.deployShell(dist.distribution);
 *
 * The deployment is exposed as a separate method so the distribution
 * (which the deployment uses to invalidate `/*` on every redeploy) can
 * be passed in once it has been constructed. Without this two-step
 * the construct order would create a circular dependency.
 */
export class McpSandboxBucketConstruct extends Construct {
  public readonly bucket: s3.Bucket;
  private readonly config: AppConfig;

  constructor(
    scope: Construct,
    id: string,
    props: McpSandboxBucketConstructProps,
  ) {
    super(scope, id);

    this.config = props.config;
    const { config } = props;

    this.bucket = new s3.Bucket(this, 'McpSandboxBucket', {
      bucketName: getResourceName(config, 'mcp-sandbox', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });
  }

  /**
   * Zip `assets/mcp-sandbox/` and upload it to the bucket, invalidating
   * the supplied CloudFront distribution's `/*` so the shell propagates
   * immediately despite the cache policy.
   *
   * Uses Source.asset on plain files (no Docker bundling) — CDK zips
   * locally and the aws-cdk-lib BucketDeployment Lambda uploads it.
   */
  public deployShell(distribution: cloudfront.IDistribution): void {
    new s3deploy.BucketDeployment(this, 'McpSandboxShellDeployment', {
      sources: [
        s3deploy.Source.asset(
          path.resolve(__dirname, '..', '..', '..', 'assets', 'mcp-sandbox'),
        ),
      ],
      destinationBucket: this.bucket,
      distribution,
      distributionPaths: ['/*'],
      prune: true,
    });
  }
}
