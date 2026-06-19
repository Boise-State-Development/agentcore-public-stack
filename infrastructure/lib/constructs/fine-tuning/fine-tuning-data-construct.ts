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

export interface FineTuningDataConstructProps {
  config: AppConfig;
}

/**
 * FineTuningDataConstruct — DynamoDB tables + S3 bucket for SageMaker
 * fine-tuning workflows.
 *
 *   - FineTuningJobsTable    — PK: USER#{userId}, SK: JOB#{jobId};
 *                              StatusIndex GSI for admin status views
 *   - FineTuningAccessTable  — PK: EMAIL#{email}, SK: ACCESS
 *   - FineTuningDataBucket   — datasets, model artifacts, inference
 *                              results. 30-day expire + 7-day multipart
 *                              abort.
 *
 * SSM publications:
 *   /{prefix}/fine-tuning/jobs-table-name
 *   /{prefix}/fine-tuning/jobs-table-arn
 *   /{prefix}/fine-tuning/access-table-name
 *   /{prefix}/fine-tuning/access-table-arn
 *   /{prefix}/fine-tuning/data-bucket-name
 *   /{prefix}/fine-tuning/data-bucket-arn
 */
export class FineTuningDataConstruct extends Construct {
  public readonly jobsTable: dynamodb.Table;
  public readonly accessTable: dynamodb.Table;
  public readonly dataBucket: s3.Bucket;

  constructor(
    scope: Construct,
    id: string,
    props: FineTuningDataConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.jobsTable = new dynamodb.Table(this, 'FineTuningJobsTable', {
      tableName: getResourceName(config, 'fine-tuning-jobs'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.jobsTable.addGlobalSecondaryIndex({
      indexName: 'StatusIndex',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
    });

    this.accessTable = new dynamodb.Table(this, 'FineTuningAccessTable', {
      tableName: getResourceName(config, 'fine-tuning-access'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    const fineTuningCorsOrigins = buildCorsOrigins(
      config,
      config.fineTuning.additionalCorsOrigins,
    );

    this.dataBucket = new s3.Bucket(this, 'FineTuningDataBucket', {
      bucketName: getResourceName(
        config,
        'fine-tuning-data',
        config.awsAccount,
      ),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
      cors:
        fineTuningCorsOrigins.length > 0
          ? [
              {
                allowedOrigins: fineTuningCorsOrigins,
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
          id: 'expire-objects',
          expiration: cdk.Duration.days(30),
        },
        {
          id: 'abort-incomplete-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
    });

    // ── SSM publications (consumed by restore tooling, app-api runtime) ──
    new ssm.StringParameter(this, 'FineTuningJobsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/jobs-table-name`,
      stringValue: this.jobsTable.tableName,
      description: 'Fine-tuning jobs table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'FineTuningAccessTableNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/access-table-name`,
      stringValue: this.accessTable.tableName,
      description: 'Fine-tuning access table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'FineTuningDataBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/fine-tuning/data-bucket-name`,
      stringValue: this.dataBucket.bucketName,
      description: 'Fine-tuning data S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
