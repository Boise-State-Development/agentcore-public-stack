import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { CfnResource } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import {
  AppConfig,
  buildCorsOrigins,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface RagDataConstructProps {
  config: AppConfig;
}

/**
 * RagDataConstruct — RAG documents bucket + vectors bucket + DDB
 * assistants table.
 *
 *   - S3 documents bucket (versioned, BLOCK_ALL public access, CORS
 *     configurable via `config.ragIngestion.additionalCorsOrigins`)
 *   - S3 Vectors bucket + index — `AWS::S3Vectors::*` (no L2 yet),
 *     dimension and distance metric driven by config; Titan V2
 *     embeddings → 1024-dim float32 cosine. The `text` metadata key
 *     is marked non-filterable because it's too large to filter on.
 *   - DynamoDB assistants table with three GSIs:
 *       OwnerStatusIndex
 *       VisibilityStatusIndex
 *       SharedWithIndex (projection = ALL)
 *
 * SSM publications:
 *   /{prefix}/rag/documents-bucket-name
 *   /{prefix}/rag/documents-bucket-arn
 *   /{prefix}/rag/assistants-table-name
 *   /{prefix}/rag/assistants-table-arn
 *   /{prefix}/rag/vector-bucket-name
 *   /{prefix}/rag/vector-index-name
 */
export class RagDataConstruct extends Construct {
  public readonly documentsBucket: s3.Bucket;
  public readonly assistantsTable: dynamodb.Table;
  public readonly vectorBucketName: string;
  public readonly vectorIndexName: string;
  public readonly vectorBucket: CfnResource;
  public readonly vectorIndex: CfnResource;

  constructor(scope: Construct, id: string, props: RagDataConstructProps) {
    super(scope, id);

    const { config } = props;

    const ragCorsOrigins = buildCorsOrigins(
      config,
      config.ragIngestion.additionalCorsOrigins,
    );

    this.documentsBucket = new s3.Bucket(this, 'RagDocumentsBucket', {
      bucketName: getResourceName(config, 'rag-documents', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
      cors:
        ragCorsOrigins.length > 0
          ? [
              {
                allowedOrigins: ragCorsOrigins,
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
    });

    this.vectorBucketName = getResourceName(
      config,
      'rag-vector-store-v1',
      config.awsAccount,
    );

    this.vectorBucket = new CfnResource(this, 'RagVectorBucket', {
      type: 'AWS::S3Vectors::VectorBucket',
      properties: {
        VectorBucketName: this.vectorBucketName,
      },
    });

    this.vectorIndexName = getResourceName(config, 'rag-vector-index-v1');

    this.vectorIndex = new CfnResource(this, 'RagVectorIndex', {
      type: 'AWS::S3Vectors::Index',
      properties: {
        VectorBucketName: this.vectorBucketName,
        IndexName: this.vectorIndexName,
        DataType: 'float32',
        Dimension: config.ragIngestion.vectorDimension,
        DistanceMetric: config.ragIngestion.vectorDistanceMetric,
        // By default, all metadata keys are filterable. Mark `text` as
        // non-filterable since it's too large for filtering — the rest
        // (assistant_id, document_id, source) stay filterable.
        MetadataConfiguration: {
          NonFilterableMetadataKeys: ['text'],
        },
      },
    });
    this.vectorIndex.addDependency(this.vectorBucket);

    this.assistantsTable = new dynamodb.Table(this, 'RagAssistantsTable', {
      tableName: getResourceName(config, 'rag-assistants'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: 'ttl',
    });

    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'OwnerStatusIndex',
      partitionKey: { name: 'GSI_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI_SK', type: dynamodb.AttributeType.STRING },
    });

    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'VisibilityStatusIndex',
      partitionKey: { name: 'GSI2_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2_SK', type: dynamodb.AttributeType.STRING },
    });

    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'SharedWithIndex',
      partitionKey: { name: 'GSI3_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI3_SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── SSM publications (consumed by restore tooling, app-api/inference-api runtime) ──
    new ssm.StringParameter(this, 'RagAssistantsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/assistants-table-name`,
      stringValue: this.assistantsTable.tableName,
      description: 'RAG assistants table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'RagDocumentsBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/documents-bucket-name`,
      stringValue: this.documentsBucket.bucketName,
      description: 'RAG documents S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
