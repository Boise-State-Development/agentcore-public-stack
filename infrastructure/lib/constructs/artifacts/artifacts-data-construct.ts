import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface ArtifactsDataConstructProps {
  config: AppConfig;
}

/**
 * ArtifactsDataConstruct — DynamoDB metadata table + S3 content bucket
 * for user-generated artifacts (HTML, code, markdown, SVG).
 *
 * DynamoDB schema:
 *   PK: USER#{user_id}
 *   SK: ARTIFACT#{artifact_id}#HEAD            (current state, 1 per artifact)
 *   SK: ARTIFACT#{artifact_id}#V#{version:05d} (immutable version records)
 *   GSI SessionIndex:
 *     PK: SESSION#{session_id}
 *     SK: ARTIFACT#{updated_at}#{artifact_id}
 *   ...lets the SPA list artifacts for the current session newest-first.
 *
 * S3 layout:
 *   {user_id}/{artifact_id}/v{n}/index.html (+ sibling assets)
 *   Private, no CORS — the iframe loads HTML directly from CloudFront
 *   (which proxies to the render Lambda), never via XHR. Versioning is
 *   at the DDB layer (immutable per-version rows + content pointer),
 *   not S3.
 *
 * Lifecycle:
 *   - Failed multipart uploads aborted after 7 days
 *   - Soft-deleted objects (tag `lifecycle-class=deleted`) reaped after
 *     `config.artifacts.retentionDays`
 *
 * SSM publications:
 *   /{prefix}/artifacts/bucket-name
 *   /{prefix}/artifacts/bucket-arn
 *   /{prefix}/artifacts/table-name
 *   /{prefix}/artifacts/table-arn
 */
export class ArtifactsDataConstruct extends Construct {
  public readonly table: dynamodb.Table;
  public readonly bucket: s3.Bucket;

  constructor(
    scope: Construct,
    id: string,
    props: ArtifactsDataConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.table = new dynamodb.Table(this, 'ArtifactsTable', {
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

    this.table.addGlobalSecondaryIndex({
      indexName: 'SessionIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.bucket = new s3.Bucket(this, 'ArtifactsContentBucket', {
      bucketName: getResourceName(config, 'artifacts-content'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: 'abort-stale-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
        {
          id: 'expire-soft-deleted',
          tagFilters: { 'lifecycle-class': 'deleted' },
          expiration: cdk.Duration.days(config.artifacts.retentionDays),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    // ── SSM publications (consumed by restore tooling, app-api runtime) ──
    new ssm.StringParameter(this, 'ArtifactsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/table-name`,
      stringValue: this.table.tableName,
      description: 'Artifacts table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ArtifactsBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'Artifacts content S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
