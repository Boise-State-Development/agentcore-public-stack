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
 *   - DynamoDB assistants table with four GSIs:
 *       OwnerStatusIndex
 *       VisibilityStatusIndex
 *       SharedWithIndex (projection = ALL)
 *       DueSyncIndex (sparse, projection = ALL — KB sync due sweep)
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

    // Sparse due-sweep index for KB sync policies: GSI4 keys exist only on
    // SYNCPOL# items while state == "active", so the sync dispatcher's query
    // physically cannot see paused policies.
    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'DueSyncIndex',
      partitionKey: { name: 'GSI4_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI4_SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Sparse marketplace directory index (docs/specs/agent-marketplace.md).
    // Same shape and same reasoning as DueSyncIndex above: GSI5 keys are written
    // only while listing.state == "published", so unpublication is enforced by
    // physics — no key, so the browse query cannot return the agent. Written
    // exclusively by apis/shared/assistants/listing_repository.py; the generic
    // assistant update lists GSI5_* as immutable so a routine author edit can
    // never resurrect a directory key on a delisted agent.
    //   GSI5_PK = LISTED#{category}   GSI5_SK = CREATED#{created_at}  (newest-first)
    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'AgentDirectoryIndex',
      partitionKey: { name: 'GSI5_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI5_SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ⚠️ TEMPORARILY REMOVED — restored by the immediately following hotfix.
    //
    // `AgentReportsIndex` (GSI6) belongs here, right after AgentDirectoryIndex
    // (GSI5). Both arrived on `develop` in separate merges, so dev picked them up
    // one deploy at a time; the 1.12.0 release collapsed them into a single CFN
    // update against an environment that had neither, and DynamoDB rejected it:
    //
    //   "Cannot perform more than one GSI creation or deletion in a single update"
    //
    // UpdateTable allows exactly one GSI create/delete per call — a limit that only
    // bites an EXISTING table (CreateTable takes as many as you like, which is why
    // the brand-new audit-log table's two indexes were fine). So GSI5 lands in this
    // deploy alone, and GSI6 is re-added in the next one once GSI5 reports ACTIVE.
    //
    // This is a deploy-sequencing artifact with a lifetime of one deploy. It is not
    // a decision about the index: nothing in the code that reads it changed, so
    // `apis/shared/assistants/reports.py` queries an index that does not exist yet
    // and the admin problem-report queue stays broken until the follow-up lands.
    // Do NOT copy this file to `develop` — develop already has both indexes live.

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
