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

    // Sparse open-report index for user problem reports (D15, Phase 8). Reports are
    // child rows of the Agent (PK = AST#{id}, SK = REPORT#{report_id}) so they are
    // deleted with it; this index is written ONLY while state == "open", so a resolved
    // or dismissed report leaves the admin queue by losing its key rather than by being
    // filtered out — the same physics as AgentDirectoryIndex above.
    //   GSI6_PK = "REPORTS#OPEN"   GSI6_SK = CREATED#{created_at}  (oldest-first sweep)
    // One partition is correct here: the open queue is bounded by how fast admins work
    // and is read only by the admin console, which wants one chronological sweep rather
    // than per-agent slices. If it ever outgrows a hot partition, that is a product
    // signal (nobody is triaging) before it is a capacity one.
    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'AgentReportsIndex',
      partitionKey: { name: 'GSI6_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI6_SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Sparse work-discovery index for managed-KB migration
    // (.kiro/specs/managed-kb-migration). Third use of the same physics as
    // DueSyncIndex and AgentDirectoryIndex above: GSI7 keys are written only while
    // a KB# record is actually eligible for background work, so a knowledge base
    // that is pinned, terminal, or simply not enrolled is invisible to the
    // dispatcher's query rather than being filtered out of it. That distinction
    // matters here because the dispatcher drives creation and deletion of billed
    // AWS resources — a filter bug would act on knowledge bases nobody asked to
    // migrate, whereas a missing key can only ever mean "do nothing".
    //   GSI7_PK = KBWORK#{state}   GSI7_SK = {dueAt ISO-8601}  (oldest-due first)
    // Written exclusively by apis/shared/kb_backend/records.py; the generic
    // assistant update lists GSI7_* as immutable, mirroring GSI5_*, so a routine
    // author edit cannot resurrect a work key on a KB that has left the queue.
    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'KbWorkIndex',
      partitionKey: { name: 'GSI7_PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI7_SK', type: dynamodb.AttributeType.STRING },
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
