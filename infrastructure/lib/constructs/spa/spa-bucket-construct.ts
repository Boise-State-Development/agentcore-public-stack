import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface SpaBucketConstructProps {
  config: AppConfig;
}

/**
 * SpaBucketConstruct — private S3 bucket holding the Angular static
 * build artifacts.
 *
 *   - Block all public access (CloudFront reaches it via OAC)
 *   - Versioned with a 30-day non-current expiration for rollback
 *   - S3-managed encryption
 *   - Bucket name derived from `config.frontend.bucketName` if set,
 *     otherwise auto-generated with the AWS account ID for global
 *     uniqueness
 *
 * Publishes the bucket name to
 * `/{prefix}/frontend/bucket-name` for the frontend deploy workflow.
 */
export class SpaBucketConstruct extends Construct {
  public readonly bucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: SpaBucketConstructProps) {
    super(scope, id);

    const { config } = props;

    const bucketName =
      config.frontend.bucketName ||
      getResourceName(config, 'frontend', config.awsAccount);

    this.bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: 'DeleteOldVersions',
          noncurrentVersionExpiration: cdk.Duration.days(30),
          enabled: true,
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    new ssm.StringParameter(this, 'BucketNameParameter', {
      parameterName: `/${config.projectPrefix}/frontend/bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'S3 bucket name for frontend assets',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
