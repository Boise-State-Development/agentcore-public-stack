import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';
import { CfnResource } from 'aws-cdk-lib';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface RagIngestionStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * RAG Ingestion Stack - Independent RAG Pipeline
 *
 * This stack creates:
 * - S3 Documents Bucket for document uploads
 * - S3 Vectors Bucket and Index for embeddings storage
 * - DynamoDB Assistants Table for metadata
 * - Lambda Function for document ingestion and embedding generation
 * - IAM roles and permissions
 * - S3 event notifications
 *
 * Dependencies:
 * - VPC and network resources from Infrastructure Stack (imported via SSM)
 * - ECR repository created by build pipeline
 *
 * Note: This is a carbon copy of the AppApiStack RAG implementation,
 * deployed as a separate modular stack with distinct resource names.
 */
export class RagIngestionStack extends cdk.Stack {
  public readonly documentsBucket: s3.Bucket;
  public readonly assistantsTable: dynamodb.Table;
  public readonly ingestionLambda: lambda.DockerImageFunction;

  constructor(scope: Construct, id: string, props: RagIngestionStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Network Resources from Infrastructure Stack
    // ============================================================

    // Import VPC
    const vpcId = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/vpc-id`
    );
    const vpcCidr = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/vpc-cidr`
    );
    const privateSubnetIdsString = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/private-subnet-ids`
    );
    const availabilityZonesString = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/availability-zones`
    );

    const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: vpcId,
      vpcCidrBlock: vpcCidr,
      availabilityZones: cdk.Fn.split(',', availabilityZonesString),
      privateSubnetIds: cdk.Fn.split(',', privateSubnetIdsString),
    });

    // Import image tag from SSM (set by push-to-ecr.sh)
    const imageTag = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag-ingestion/image-tag`
    );

    // ============================================================
    // S3 Documents Bucket
    // ============================================================

    const corsOrigins = config.ragIngestion.corsOrigins
      ? config.ragIngestion.corsOrigins.split(',').map((o) => o.trim())
      : [];

    this.documentsBucket = new s3.Bucket(this, 'RagDocumentsBucket', {
      bucketName: getResourceName(config, 'rag-documents'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
      cors: corsOrigins.length > 0 ? [
        {
          allowedOrigins: corsOrigins,
          allowedMethods: [
            s3.HttpMethods.GET,
            s3.HttpMethods.PUT,
            s3.HttpMethods.HEAD,
          ],
          allowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
          exposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
          maxAge: 3600,
        },
      ] : undefined,
    });

    // ============================================================
    // S3 Vectors Bucket and Index
    // ============================================================

    // Create S3 Vector Bucket (using CfnResource since there are no L2 constructs yet)
    const vectorBucketName = getResourceName(config, 'rag-vector-store-v1');

    const vectorBucket = new CfnResource(this, 'RagVectorBucket', {
      type: 'AWS::S3Vectors::VectorBucket',
      properties: {
        VectorBucketName: vectorBucketName,
      },
    });

    // Create Vector Index within the bucket
    // Titan V2 embeddings: 1024 dimensions, float32, cosine similarity
    const vectorIndexName = getResourceName(config, 'rag-vector-index-v1');

    const vectorIndex = new CfnResource(this, 'RagVectorIndex', {
      type: 'AWS::S3Vectors::Index',
      properties: {
        VectorBucketName: vectorBucketName,
        IndexName: vectorIndexName,
        DataType: 'float32', // Only supported type
        Dimension: config.ragIngestion.vectorDimension,
        DistanceMetric: config.ragIngestion.vectorDistanceMetric,
        // MetadataConfiguration: Specify which metadata keys are NOT filterable
        // By default, all metadata keys (assistant_id, document_id, source) are filterable
        // Only mark 'text' as non-filterable since it's too large for filtering
        MetadataConfiguration: {
          NonFilterableMetadataKeys: ['text'],
        },
      },
    });

    // Index depends on bucket
    vectorIndex.addDependency(vectorBucket);

    // ============================================================
    // DynamoDB Assistants Table
    // ============================================================

    this.assistantsTable = new dynamodb.Table(this, 'RagAssistantsTable', {
      tableName: getResourceName(config, 'rag-assistants'),
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy:
        config.environment === 'prod'
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Add Global Secondary Indexes
    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'OwnerStatusIndex',
      partitionKey: {
        name: 'GSI_PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI_SK',
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'VisibilityStatusIndex',
      partitionKey: {
        name: 'GSI2_PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2_SK',
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.assistantsTable.addGlobalSecondaryIndex({
      indexName: 'SharedWithIndex',
      partitionKey: {
        name: 'GSI3_PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3_SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ============================================================
    // Lambda Function for Document Ingestion
    // ============================================================

    // Reference the ECR repository created by the build pipeline
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'RagIngestionRepository',
      getResourceName(config, 'rag-ingestion')
    );

    const containerImageUri = `${ecrRepository.repositoryUri}:${imageTag}`;

    this.ingestionLambda = new lambda.DockerImageFunction(
      this,
      'RagIngestionLambda',
      {
        functionName: getResourceName(config, 'rag-ingestion'),
        code: lambda.DockerImageCode.fromEcr(ecrRepository, {
          tagOrDigest: imageTag,
        }),
        architecture: lambda.Architecture.ARM_64, // ARM64 (Graviton2) for better price/performance
        timeout: cdk.Duration.seconds(config.ragIngestion.lambdaTimeout),
        memorySize: config.ragIngestion.lambdaMemorySize,
        environment: {
          ASSISTANTS_DOCUMENTS_BUCKET_NAME: this.documentsBucket.bucketName,
          ASSISTANTS_TABLE_NAME: this.assistantsTable.tableName,
          ASSISTANTS_VECTOR_STORE_BUCKET_NAME: vectorBucketName,
          ASSISTANTS_VECTOR_STORE_INDEX_NAME: vectorIndexName,
          BEDROCK_REGION: config.awsRegion,
        },
        description:
          'RAG document ingestion pipeline - processes documents from S3, extracts text, chunks, generates embeddings, stores in S3 vector store',
      }
    );

    // ============================================================
    // IAM Permissions
    // ============================================================

    // Grant Lambda read permission on Documents Bucket
    this.documentsBucket.grantRead(this.ingestionLambda);

    // Grant Lambda read/write permission on Assistants Table
    this.assistantsTable.grantReadWriteData(this.ingestionLambda);

    // Grant Lambda full access to the vector bucket and index
    this.ingestionLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3vectors:ListVectorBuckets',
          's3vectors:GetVectorBucket',
          's3vectors:GetIndex',
          's3vectors:PutVectors',
          's3vectors:ListVectors',
          's3vectors:ListIndexes',
          's3vectors:GetVector',
          's3vectors:GetVectors',
          's3vectors:DeleteVector',
        ],
        resources: [
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}`,
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}/index/${vectorIndexName}`,
        ],
      })
    );

    // Grant Lambda permission to invoke Bedrock model for embeddings
    this.ingestionLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${config.awsRegion}::foundation-model/${config.ragIngestion.embeddingModel}*`,
        ],
      })
    );

    // ============================================================
    // S3 Event Notifications
    // ============================================================

    // Configure S3 event trigger to trigger the Lambda function when objects are created
    // in the documents bucket with prefix "assistants/"
    this.documentsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(this.ingestionLambda),
      {
        prefix: 'assistants/',
      }
    );

    // ============================================================
    // SSM Parameter Exports
    // ============================================================

    new ssm.StringParameter(this, 'DocumentsBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/documents-bucket-name`,
      stringValue: this.documentsBucket.bucketName,
      description: 'RAG documents bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'DocumentsBucketArnParameter', {
      parameterName: `/${config.projectPrefix}/rag/documents-bucket-arn`,
      stringValue: this.documentsBucket.bucketArn,
      description: 'RAG documents bucket ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'AssistantsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/assistants-table-name`,
      stringValue: this.assistantsTable.tableName,
      description: 'RAG assistants table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'AssistantsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/rag/assistants-table-arn`,
      stringValue: this.assistantsTable.tableArn,
      description: 'RAG assistants table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'VectorBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/vector-bucket-name`,
      stringValue: vectorBucketName,
      description: 'RAG vector store bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'VectorIndexNameParameter', {
      parameterName: `/${config.projectPrefix}/rag/vector-index-name`,
      stringValue: vectorIndexName,
      description: 'RAG vector store index name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'IngestionLambdaArnParameter', {
      parameterName: `/${config.projectPrefix}/rag/ingestion-lambda-arn`,
      stringValue: this.ingestionLambda.functionArn,
      description: 'RAG ingestion Lambda ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================

    new cdk.CfnOutput(this, 'DocumentsBucketName', {
      value: this.documentsBucket.bucketName,
      description: 'RAG documents bucket name',
      exportName: `${config.projectPrefix}-RagDocumentsBucketName`,
    });

    new cdk.CfnOutput(this, 'AssistantsTableName', {
      value: this.assistantsTable.tableName,
      description: 'RAG assistants table name',
      exportName: `${config.projectPrefix}-RagAssistantsTableName`,
    });

    new cdk.CfnOutput(this, 'IngestionLambdaArn', {
      value: this.ingestionLambda.functionArn,
      description: 'RAG ingestion Lambda function ARN',
      exportName: `${config.projectPrefix}-RagIngestionLambdaArn`,
    });

    new cdk.CfnOutput(this, 'VectorBucketName', {
      value: vectorBucketName,
      description: 'RAG vector store bucket name',
      exportName: `${config.projectPrefix}-RagVectorBucketName`,
    });

    new cdk.CfnOutput(this, 'VectorIndexName', {
      value: vectorIndexName,
      description: 'RAG vector store index name',
      exportName: `${config.projectPrefix}-RagVectorIndexName`,
    });
  }
}
