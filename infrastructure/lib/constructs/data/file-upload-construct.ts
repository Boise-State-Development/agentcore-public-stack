import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  buildCorsOrigins,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface FileUploadConstructProps {
  config: AppConfig;
}

/**
 * FileUploadConstruct — user file uploads bucket + metadata table.
 *
 * Lives in PlatformStack so both app-api (uploads) and inference-api
 * (reads) consume the same resources without a circular stack
 * dependency: inference-api (compute) deploys before app-api (compute)
 * but both depend on this single shared bucket + table pair.
 *
 * Schema:
 *   PK: USER#{userId}, SK: FILE#{uploadId}     - File metadata
 *   PK: USER#{userId}, SK: QUOTA               - User storage quota
 *   GSI1: SessionIndex
 *     GSI1PK: CONV#{sessionId}, GSI1SK: FILE#{uploadId}
 *     - Query files by conversation
 *
 * S3 lifecycle:
 *   Day 0  → S3_STANDARD
 *   Day 30 → STANDARD_IA
 *   Day 90 → GLACIER_INSTANT_RETRIEVAL
 *   Day {fileUpload.retentionDays} → expired
 */
export class FileUploadConstruct extends Construct {
  public readonly bucket: s3.Bucket;
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: FileUploadConstructProps) {
    super(scope, id);

    const { config } = props;

    const fileUploadCorsOrigins = buildCorsOrigins(config);

    this.bucket = new s3.Bucket(this, 'UserFilesBucket', {
      bucketName: getResourceName(
        config,
        'user-file-uploads',
        config.awsAccount,
      ),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
      cors:
        fileUploadCorsOrigins.length > 0
          ? [
              {
                allowedOrigins: fileUploadCorsOrigins,
                allowedMethods: [
                  s3.HttpMethods.GET,
                  s3.HttpMethods.PUT,
                  s3.HttpMethods.HEAD,
                ],
                allowedHeaders: [
                  'Content-Type',
                  'Content-Length',
                  'x-amz-*',
                ],
                exposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
                maxAge: 3600,
              },
            ]
          : undefined,
      lifecycleRules: [
        {
          id: 'transition-to-ia',
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
        {
          id: 'transition-to-glacier',
          transitions: [
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: cdk.Duration.days(90),
            },
          ],
        },
        {
          id: 'expire-objects',
          expiration: cdk.Duration.days(365),
        },
        {
          id: 'abort-incomplete-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
    });

    this.table = new dynamodb.Table(this, 'UserFilesTable', {
      tableName: getResourceName(config, 'user-file-uploads'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'ttl',
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: getRemovalPolicy(config),
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'SessionIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── SSM publications (consumed by restore tooling, app-api runtime) ──
    new ssm.StringParameter(this, 'FileUploadTableNameParameter', {
      parameterName: `/${config.projectPrefix}/user-file-uploads/table-name`,
      stringValue: this.table.tableName,
      description: 'User file uploads table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'FileUploadBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/user-file-uploads/bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'User file uploads S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
