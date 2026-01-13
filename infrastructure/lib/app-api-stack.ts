import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { CfnResource } from 'aws-cdk-lib';
import { AppConfig, getResourceName, applyStandardTags } from './config';
import * as path from 'path';

// __dirname is available in CommonJS runtime, but TypeScript needs a declaration
declare const __dirname: string;

export interface AppApiStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * App API Stack - Core Backend Application
 * 
 * This stack creates:
 * - ECS Fargate service for App API
 * - Target group and listener rules for ALB routing
 * - Database (DynamoDB or RDS Aurora Serverless v2)
 * - Security groups for ECS tasks
 * 
 * Dependencies:
 * - VPC, ALB, ECS Cluster from Infrastructure Stack (imported via SSM)
 * 
 * Note: ECR repository is created by the build pipeline, not by CDK.
 */
export class AppApiStack extends cdk.Stack {
  public readonly ecsService: ecs.FargateService;

  constructor(scope: Construct, id: string, props: AppApiStackProps) {
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

    // Import image tag from SSM (set by push-to-ecr.sh)
    const imageTag = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/app-api/image-tag`
    );

    // Import authentication secret ARN from SSM (created by infrastructure stack)
    const authSecretArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/secret-arn`
    );

    const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: vpcId,
      vpcCidrBlock: vpcCidr,
      availabilityZones: cdk.Fn.split(',', availabilityZonesString),
      privateSubnetIds: cdk.Fn.split(',', privateSubnetIdsString),
    });

    // Import ALB Security Group
    const albSecurityGroupId = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-security-group-id`
    );
    const albSecurityGroup = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedAlbSecurityGroup',
      albSecurityGroupId
    );

    // Import ALB
    const albArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-arn`
    );
    const alb = elbv2.ApplicationLoadBalancer.fromApplicationLoadBalancerAttributes(
      this,
      'ImportedAlb',
      {
        loadBalancerArn: albArn,
        securityGroupId: albSecurityGroupId,
      }
    );

    // Import ALB Listener
    const albListenerArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/alb-listener-arn`
    );
    const albListener = elbv2.ApplicationListener.fromApplicationListenerAttributes(
      this,
      'ImportedAlbListener',
      {
        listenerArn: albListenerArn,
        securityGroup: albSecurityGroup,
      }
    );

    // Import ECS Cluster
    const ecsClusterName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/ecs-cluster-name`
    );
    const ecsClusterArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/network/ecs-cluster-arn`
    );
    const ecsCluster = ecs.Cluster.fromClusterAttributes(this, 'ImportedEcsCluster', {
      clusterName: ecsClusterName,
      clusterArn: ecsClusterArn,
      vpc: vpc,
      securityGroups: [],
    });

    // ============================================================
    // Security Groups
    // ============================================================
    
    // ECS Task Security Group - Allow traffic from ALB
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'AppEcsSecurityGroup', {
      vpc: vpc,
      securityGroupName: getResourceName(config, 'app-ecs-sg'),
      description: 'Security group for App API ECS Fargate tasks',
      allowAllOutbound: true,
    });

    ecsSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8000),
      'Allow traffic from ALB to App API tasks'
    );

    // ============================================================
    // Assistants Table
    // Base Table PK (String) SK (String)
    // Owner Status Index GSI_PK (String) GSI_SK (String)
    // Visibility Status Index GSI2_PK (String) GSI2_SK (String)
    // ============================================================
    const assistantsTable = new dynamodb.Table(this, 'AssistantsTable', {
      tableName: getResourceName(config, 'assistants'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    assistantsTable.addGlobalSecondaryIndex({
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

    assistantsTable.addGlobalSecondaryIndex({
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
    
    // ============================================================
    // Assistants Document Drop Bucket (RAG Injestion Drop Bucket)
    // ============================================================
    const origins = config.assistants?.corsOrigins.split(',').map(o => o.trim())
    

    const assistantsDocumentsBucket = new s3.Bucket(this, 'AssistantsDocumentBucket', {
      bucketName: getResourceName(config, 'assistants-documents'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: false,
      cors: [{
        allowedOrigins: config.assistants?.corsOrigins.split(',').map(o => o.trim()),
        allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
        allowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
        exposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
        maxAge: 3600,
      }],
    });

    // ============================================================
    // Assistants Vector Store Bucket
    // ============================================================
    // Create S3 Vector Bucket (not a regular S3 bucket)
    // Using CfnResource since there are no L2 constructs for S3 Vectors yet
    // Bucket name: 3-63 chars, lowercase, numbers, hyphens only
    const assistantsVectorStoreBucketName = getResourceName(config, 'assistants-vector-store-v1');
    
    const assistantsVectorBucket = new CfnResource(this, 'AssistantsVectorBucket', {
      type: 'AWS::S3Vectors::VectorBucket',
      properties: {
        VectorBucketName: assistantsVectorStoreBucketName,
      },
    });

    // Create Vector Index within the bucket
    // Titan V2 embeddings: 1024 dimensions, float32, cosine similarity
    const assistantsVectorIndexName = getResourceName(config, 'assistants-vector-index-v1');
    
    const assistantsVectorIndex = new CfnResource(this, 'AssistantsVectorIndex', {
      type: 'AWS::S3Vectors::Index',
      properties: {
        VectorBucketName: assistantsVectorStoreBucketName,
        IndexName: assistantsVectorIndexName,
        DataType: 'float32', // Only supported type
        Dimension: 1024, // Titan V2 embedding dimension
        DistanceMetric: 'cosine', // Cosine similarity for embeddings
        // MetadataConfiguration: Specify which metadata keys are NOT filterable
        // By default, all metadata keys (assistant_id, document_id, source) are filterable
        // Only mark 'text' as non-filterable since it's too large for filtering
        MetadataConfiguration: {
          NonFilterableMetadataKeys: ['text'],
        },
      },
    });

    // Index depends on bucket
    assistantsVectorIndex.addDependency(assistantsVectorBucket);

    // ============================================================
    // Assistants Document Ingestion Lambda Function
    // ============================================================

    // Create Lambda function using container image
    // Container images support up to 10GB (vs 250MB per layer limit)
    // Dockerfile is in backend/ directory
    // Build context is set to repo root so we can access backend/ directory
    // __dirname is infrastructure/lib/, so go up 2 levels to get repo root
    const repoRoot = path.join(__dirname, '../..'); // infrastructure/lib/ -> infrastructure/ -> repo root
    const dockerfilePath = path.join(repoRoot, 'backend', 'Dockerfile.rag-ingestion');

    const assistantsDocumentsIngestionlambdaFunction = new lambda.DockerImageFunction(this, 'AssistantsDocumentsIngestionlambdaFunction', {
      code: lambda.DockerImageCode.fromImageAsset(repoRoot, {
        file: path.relative(repoRoot, dockerfilePath),
      }),
      architecture: lambda.Architecture.ARM_64, // ARM64 (Graviton2) is ~20% cheaper and better price/performance for Lambda
      timeout: cdk.Duration.minutes(15),
      memorySize: 10240, // Increased to max for heavy ML libraries and to speed up init/invoke
      environment: {
        ASSISTANTS_DOCUMENTS_BUCKET_NAME: assistantsDocumentsBucket.bucketName,
        ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        ASSISTANTS_VECTOR_STORE_BUCKET_NAME: assistantsVectorStoreBucketName,
        ASSISTANTS_VECTOR_STORE_INDEX_NAME: assistantsVectorIndexName, 
        BEDROCK_REGION: config.awsRegion,
      },
      description: 'Assistants document ingestion pipeline - processes documents from S3, extracts text, chunks, generates embeddings, stores in S3 vector store',
    });

    // ============================================================
    // Assistants Permissions
    // ============================================================

    // Give the lambda function permission to read from the documents bucket
    assistantsDocumentsBucket.grantRead(assistantsDocumentsIngestionlambdaFunction);
    // Give the lambda function permission to write to the assistants table
    assistantsTable.grantWriteData(assistantsDocumentsIngestionlambdaFunction);
    // Give the lambda function permission to read from the assistants table
    assistantsTable.grantReadData(assistantsDocumentsIngestionlambdaFunction);

    // Give the lambda function full access to the vector bucket and index
    assistantsDocumentsIngestionlambdaFunction.addToRolePolicy(
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
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${assistantsVectorStoreBucketName}`,
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${assistantsVectorStoreBucketName}/index/${assistantsVectorIndexName}`,
        ],
      })
    );

    // Give the lambda function permission to invoke the Bedrock model for embeddings
    assistantsDocumentsIngestionlambdaFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.titan-embed-text-v2*`,
        ],
      })
    );

    // Configure S3 event trigger to trigger the lambda function when objects are created in the documents bucket with prefix "assistants/"
    // Trigger Lambda when objects are created in documents bucket with prefix "assistants/"
    assistantsDocumentsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(assistantsDocumentsIngestionlambdaFunction),
      { prefix: 'assistants/' }
    );
    
    // ============================================================
    // Quota Management Tables
    // ============================================================

    // UserQuotas Table
    const userQuotasTable = new dynamodb.Table(this, 'UserQuotasTable', {
      tableName: getResourceName(config, 'user-quotas'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: AssignmentTypeIndex - Query assignments by type, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AssignmentTypeIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: UserAssignmentIndex - Query direct user assignments (O(1) lookup)
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserAssignmentIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI3: RoleAssignmentIndex - Query role-based assignments, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'RoleAssignmentIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI4: UserOverrideIndex - Query active overrides by user, sorted by expiry
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserOverrideIndex',
      partitionKey: {
        name: 'GSI4PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI4SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // QuotaEvents Table
    const quotaEventsTable = new dynamodb.Table(this, 'QuotaEventsTable', {
      tableName: getResourceName(config, 'quota-events'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI5: TierEventIndex - Query events by tier for analytics
    quotaEventsTable.addGlobalSecondaryIndex({
      indexName: 'TierEventIndex',
      partitionKey: {
        name: 'GSI5PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI5SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store quota table names in SSM
    new ssm.StringParameter(this, 'UserQuotasTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-name`,
      stringValue: userQuotasTable.tableName,
      description: 'UserQuotas table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserQuotasTableArnParameter', {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-arn`,
      stringValue: userQuotasTable.tableArn,
      description: 'UserQuotas table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'QuotaEventsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-name`,
      stringValue: quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'QuotaEventsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-arn`,
      stringValue: quotaEventsTable.tableArn,
      description: 'QuotaEvents table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Cost Tracking Tables
    // ============================================================

    // SessionsMetadata Table - Message-level metadata for cost tracking
    const sessionsMetadataTable = new dynamodb.Table(this, 'SessionsMetadataTable', {
      tableName: getResourceName(config, 'sessions-metadata'),
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
      timeToLiveAttribute: 'ttl',
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: UserTimestampIndex - Query messages by user and time range
    sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: 'UserTimestampIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // SessionLookupIndex - Direct session lookup by ID and per-session cost queries
    // Enables:
    //   - O(1) session lookup: GSI_PK=SESSION#{session_id}, GSI_SK=META
    //   - Per-session costs: GSI_PK=SESSION#{session_id}, GSI_SK begins_with C#
    sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: 'SessionLookupIndex',
      partitionKey: {
        name: 'GSI_PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI_SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // UserCostSummary Table - Pre-aggregated cost summaries for fast quota checks
    const userCostSummaryTable = new dynamodb.Table(this, 'UserCostSummaryTable', {
      tableName: getResourceName(config, 'user-cost-summary'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI2: PeriodCostIndex - Query top users by cost for admin dashboard
    // Enables efficient "top N users by cost" queries without table scans
    userCostSummaryTable.addGlobalSecondaryIndex({
      indexName: 'PeriodCostIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['userId', 'totalCost', 'totalRequests', 'lastUpdated'],
    });

    // SystemCostRollup Table - Pre-aggregated system-wide metrics for admin dashboard
    const systemCostRollupTable = new dynamodb.Table(this, 'SystemCostRollupTable', {
      tableName: getResourceName(config, 'system-cost-rollup'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Store cost tracking table names in SSM
    new ssm.StringParameter(this, 'SessionsMetadataTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-name`,
      stringValue: sessionsMetadataTable.tableName,
      description: 'SessionsMetadata table name for message-level cost tracking',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SessionsMetadataTableArnParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-arn`,
      stringValue: sessionsMetadataTable.tableArn,
      description: 'SessionsMetadata table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserCostSummaryTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-name`,
      stringValue: userCostSummaryTable.tableName,
      description: 'UserCostSummary table name for aggregated cost summaries',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserCostSummaryTableArnParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-arn`,
      stringValue: userCostSummaryTable.tableArn,
      description: 'UserCostSummary table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SystemCostRollupTableNameParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-name`,
      stringValue: systemCostRollupTable.tableName,
      description: 'SystemCostRollup table name for admin dashboard aggregates',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SystemCostRollupTableArnParameter', {
      parameterName: `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-arn`,
      stringValue: systemCostRollupTable.tableArn,
      description: 'SystemCostRollup table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // OIDC State Management Table
    // ============================================================

    // OidcState Table - Distributed state storage for OIDC authentication
    const oidcStateTable = new dynamodb.Table(this, 'OidcStateTable', {
      tableName: getResourceName(config, 'oidc-state'),
      partitionKey: {
        name: 'PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'SK',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiresAt',
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Store OIDC state table name in SSM
    new ssm.StringParameter(this, 'OidcStateTableNameParameter', {
      parameterName: `/${config.projectPrefix}/auth/oidc-state-table-name`,
      stringValue: oidcStateTable.tableName,
      description: 'OIDC state table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'OidcStateTableArnParameter', {
      parameterName: `/${config.projectPrefix}/auth/oidc-state-table-arn`,
      stringValue: oidcStateTable.tableArn,
      description: 'OIDC state table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Managed Models Table
    // ============================================================

    // ManagedModels Table - Model management and pricing data
    const managedModelsTable = new dynamodb.Table(this, 'ManagedModelsTable', {
      tableName: getResourceName(config, 'managed-models'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: ModelIdIndex - Query by modelId for duplicate checking
    managedModelsTable.addGlobalSecondaryIndex({
      indexName: 'ModelIdIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store managed models table name in SSM
    new ssm.StringParameter(this, 'ManagedModelsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/admin/managed-models-table-name`,
      stringValue: managedModelsTable.tableName,
      description: 'Managed models table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ManagedModelsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/admin/managed-models-table-arn`,
      stringValue: managedModelsTable.tableArn,
      description: 'Managed models table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Users Table (User Admin)
    // ============================================================

    // Users Table - User profiles synced from JWT for admin lookup
    const usersTable = new dynamodb.Table(this, 'UsersTable', {
      tableName: getResourceName(config, 'users'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // UserIdIndex - O(1) lookup by userId for admin deep links
    usersTable.addGlobalSecondaryIndex({
      indexName: 'UserIdIndex',
      partitionKey: {
        name: 'userId',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // EmailIndex - O(1) lookup by email for search
    usersTable.addGlobalSecondaryIndex({
      indexName: 'EmailIndex',
      partitionKey: {
        name: 'email',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // EmailDomainIndex - Browse users by company/domain, sorted by last login
    usersTable.addGlobalSecondaryIndex({
      indexName: 'EmailDomainIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['userId', 'email', 'name', 'status'],
    });

    // StatusLoginIndex - Browse users by status, sorted by last login
    usersTable.addGlobalSecondaryIndex({
      indexName: 'StatusLoginIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['userId', 'email', 'name', 'emailDomain'],
    });

    // Store users table name in SSM
    new ssm.StringParameter(this, 'UsersTableNameParameter', {
      parameterName: `/${config.projectPrefix}/users/users-table-name`,
      stringValue: usersTable.tableName,
      description: 'Users table name for admin user lookup',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UsersTableArnParameter', {
      parameterName: `/${config.projectPrefix}/users/users-table-arn`,
      stringValue: usersTable.tableArn,
      description: 'Users table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // AppRoles Table (RBAC)
    // ============================================================

    // AppRoles Table - Role definitions and permission mappings
    const appRolesTable = new dynamodb.Table(this, 'AppRolesTable', {
      tableName: getResourceName(config, 'app-roles'),
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
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: JwtRoleMappingIndex - Fast lookup: "Given JWT role X, what AppRoles apply?"
    // This is the critical index for authorization performance
    appRolesTable.addGlobalSecondaryIndex({
      indexName: 'JwtRoleMappingIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: ToolRoleMappingIndex - Reverse lookup: "What AppRoles grant access to tool X?"
    // Used for bidirectional sync when updating tool permissions
    appRolesTable.addGlobalSecondaryIndex({
      indexName: 'ToolRoleMappingIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
    });

    // GSI3: ModelRoleMappingIndex - Reverse lookup: "What AppRoles grant access to model X?"
    // Used for bidirectional sync when updating model permissions
    appRolesTable.addGlobalSecondaryIndex({
      indexName: 'ModelRoleMappingIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
    });

    // Store AppRoles table name in SSM
    new ssm.StringParameter(this, 'AppRolesTableNameParameter', {
      parameterName: `/${config.projectPrefix}/rbac/app-roles-table-name`,
      stringValue: appRolesTable.tableName,
      description: 'AppRoles table name for RBAC',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'AppRolesTableArnParameter', {
      parameterName: `/${config.projectPrefix}/rbac/app-roles-table-arn`,
      stringValue: appRolesTable.tableArn,
      description: 'AppRoles table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // File Upload Storage (S3 + DynamoDB)
    // ============================================================

    // Determine allowed CORS origins based on environment
    const fileUploadCorsOrigins = config.fileUpload?.corsOrigins
      ? config.fileUpload.corsOrigins.split(',').map(o => o.trim())
      : config.environment === 'prod'
        ? ['https://boisestate.ai', 'https://*.boisestate.ai']
        : ['http://localhost:4200', 'http://localhost:8000'];

    // S3 Bucket for user file uploads
    const userFilesBucket = new s3.Bucket(this, 'UserFilesBucket', {
      // Include account ID for global uniqueness
      bucketName: getResourceName(config, 'user-files', config.awsAccount),

      // Security configuration
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,

      // Retain in prod, allow destroy in dev
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: config.environment !== 'prod',

      // CORS for browser-based pre-signed URL uploads
      cors: [{
        allowedOrigins: fileUploadCorsOrigins,
        allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
        allowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
        exposedHeaders: ['ETag', 'Content-Length', 'Content-Type'],
        maxAge: 3600,
      }],

      // Intelligent tiering lifecycle rules
      lifecycleRules: [
        {
          id: 'transition-to-ia',
          transitions: [{
            storageClass: s3.StorageClass.INFREQUENT_ACCESS,
            transitionAfter: cdk.Duration.days(30),
          }],
        },
        {
          id: 'transition-to-glacier',
          transitions: [{
            storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
            transitionAfter: cdk.Duration.days(90),
          }],
        },
        {
          id: 'expire-objects',
          expiration: cdk.Duration.days(config.fileUpload?.retentionDays || 365),
        },
        {
          id: 'abort-incomplete-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
    });

    // DynamoDB Table for file metadata
    /**
     * Schema:
     *   PK: USER#{userId}, SK: FILE#{uploadId} - File metadata
     *   PK: USER#{userId}, SK: QUOTA - User storage quota tracking
     *   GSI1PK: CONV#{sessionId}, GSI1SK: FILE#{uploadId} - Query files by conversation
     */
    const userFilesTable = new dynamodb.Table(this, 'UserFilesTable', {
      tableName: getResourceName(config, 'user-files'),
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
      timeToLiveAttribute: 'ttl',
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: config.environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    // GSI1: SessionIndex - Query files by conversation/session
    userFilesTable.addGlobalSecondaryIndex({
      indexName: 'SessionIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store file upload resource names in SSM
    new ssm.StringParameter(this, 'UserFilesBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/file-upload/bucket-name`,
      stringValue: userFilesBucket.bucketName,
      description: 'User files S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserFilesBucketArnParameter', {
      parameterName: `/${config.projectPrefix}/file-upload/bucket-arn`,
      stringValue: userFilesBucket.bucketArn,
      description: 'User files S3 bucket ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserFilesTableNameParameter', {
      parameterName: `/${config.projectPrefix}/file-upload/table-name`,
      stringValue: userFilesTable.tableName,
      description: 'User files metadata table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserFilesTableArnParameter', {
      parameterName: `/${config.projectPrefix}/file-upload/table-arn`,
      stringValue: userFilesTable.tableArn,
      description: 'User files metadata table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // ECS Task Definition
    // ============================================================
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-app-api/push-to-ecr.sh)
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'AppApiTaskDefinition', {
      family: getResourceName(config, 'app-api-task'),
      cpu: config.appApi.cpu,
      memoryLimitMiB: config.appApi.memory,
    });

    // Create log group for ECS task
    const logGroup = new logs.LogGroup(this, 'AppApiLogGroup', {
      logGroupName: `/ecs/${config.projectPrefix}/app-api`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Reference the ECR repository created by the build pipeline
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'AppApiRepository',
      getResourceName(config, 'app-api')
    );

    // Reference the authentication secret from infrastructure stack
    const authSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this,
      'AuthSecret',
      authSecretArn
    );

    // Container Definition
    const container = taskDefinition.addContainer('AppApiContainer', {
      containerName: 'app-api',
      image: ecs.ContainerImage.fromEcrRepository(ecrRepository, imageTag),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'app-api',
        logGroup: logGroup,
      }),
      environment: {
        AWS_REGION: config.awsRegion,
        PROJECT_PREFIX: config.projectPrefix,
        DYNAMODB_QUOTA_TABLE: userQuotasTable.tableName,
        DYNAMODB_EVENTS_TABLE: quotaEventsTable.tableName,
        DYNAMODB_OIDC_STATE_TABLE_NAME: oidcStateTable.tableName,
        DYNAMODB_MANAGED_MODELS_TABLE_NAME: managedModelsTable.tableName,
        DYNAMODB_SESSIONS_METADATA_TABLE_NAME: sessionsMetadataTable.tableName,
        DYNAMODB_COST_SUMMARY_TABLE_NAME: userCostSummaryTable.tableName,
        DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: systemCostRollupTable.tableName,
        DYNAMODB_USERS_TABLE_NAME: usersTable.tableName,
        DYNAMODB_APP_ROLES_TABLE_NAME: appRolesTable.tableName,
        DYNAMODB_USER_FILES_TABLE_NAME: userFilesTable.tableName,
        S3_USER_FILES_BUCKET_NAME: userFilesBucket.bucketName,
        FILE_UPLOAD_MAX_SIZE_BYTES: String(config.fileUpload?.maxFileSizeBytes || 4194304),
        FILE_UPLOAD_MAX_FILES_PER_MESSAGE: String(config.fileUpload?.maxFilesPerMessage || 5),
        FILE_UPLOAD_USER_QUOTA_BYTES: String(config.fileUpload?.userQuotaBytes || 1073741824),
        ASSISTANTS_DOCUMENTS_BUCKET_NAME: assistantsDocumentsBucket.bucketName,
        ASSISTANTS_TABLE_NAME: assistantsTable.tableName,
        ASSISTANTS_VECTOR_STORE_BUCKET_NAME: assistantsVectorStoreBucketName,
        ASSISTANTS_VECTOR_STORE_INDEX_NAME: assistantsVectorIndexName, 
        ENTRA_CLIENT_ID: config.appApi.entraClientId,
        ENTRA_TENANT_ID: config.appApi.entraTenantId,
        ENTRA_REDIRECT_URI: config.appApi.entraRedirectUri,
        // DATABASE_TYPE: config.appApi.databaseType,
        // ...(databaseConnectionInfo && { DATABASE_CONNECTION: databaseConnectionInfo }),
      },
      secrets: {
        // Secret values fetched at task startup, never visible in console/API
        ENTRA_CLIENT_SECRET: ecs.Secret.fromSecretsManager(authSecret, 'secret'),
      },
      portMappings: [
        {
          containerPort: 8000,
          protocol: ecs.Protocol.TCP,
        },
      ],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // Grant permissions for database access
    if (config.appApi.databaseType === 'dynamodb') {
      // Grant DynamoDB permissions (will be added after table is created)
      // This is a placeholder - actual permissions will be granted via IAM policy
    }

    // Grant permissions for quota management tables
    userQuotasTable.grantReadWriteData(taskDefinition.taskRole);
    quotaEventsTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for OIDC state table
    oidcStateTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for managed models table
    managedModelsTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for cost tracking tables
    sessionsMetadataTable.grantReadWriteData(taskDefinition.taskRole);
    userCostSummaryTable.grantReadWriteData(taskDefinition.taskRole);
    systemCostRollupTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for users table
    usersTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for AppRoles table
    appRolesTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for file upload resources
    userFilesTable.grantReadWriteData(taskDefinition.taskRole);
    userFilesBucket.grantReadWrite(taskDefinition.taskRole);

    // Grant permissions for authentication secret (imported from infrastructure stack)
    // Using manual policy statement for clarity when working with cross-stack imported secrets
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
        ],
        resources: [authSecretArn],
      })
    );

    // ============================================================
    // Target Group
    // ============================================================
    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppApiTargetGroup', {
      vpc: vpc,
      targetGroupName: getResourceName(config, 'app-api-tg'),
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        enabled: true,
        path: '/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: '200',
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // Add listener rule for App API (all traffic)
    // Since this is the only target, route all HTTPS traffic to this target group
    albListener.addTargetGroups('AppApiTargetGroupAttachment', {
      targetGroups: [targetGroup],
      priority: 1,
      conditions: [
        elbv2.ListenerCondition.pathPatterns(['/*']),
      ],
    });

    // ============================================================
    // ECS Fargate Service
    // ============================================================
    this.ecsService = new ecs.FargateService(this, 'AppApiService', {
      cluster: ecsCluster,
      serviceName: getResourceName(config, 'app-api-service'),
      taskDefinition: taskDefinition,
      desiredCount: config.appApi.desiredCount,
      securityGroups: [ecsSecurityGroup],
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      assignPublicIp: false,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
      circuitBreaker: {
        rollback: true,
      },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // Attach service to target group
    this.ecsService.attachToApplicationTargetGroup(targetGroup);

    // Auto-scaling configuration
    const scaling = this.ecsService.autoScaleTaskCount({
      minCapacity: config.appApi.desiredCount,
      maxCapacity: config.appApi.maxCapacity,
    });

    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    scaling.scaleOnMemoryUtilization('MemoryScaling', {
      targetUtilizationPercent: 80,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    new cdk.CfnOutput(this, 'EcsServiceName', {
      value: this.ecsService.serviceName,
      description: 'ECS Service Name',
      exportName: `${config.projectPrefix}-AppEcsServiceName`,
    });

    new cdk.CfnOutput(this, 'TaskDefinitionArn', {
      value: taskDefinition.taskDefinitionArn,
      description: 'Task Definition ARN',
      exportName: `${config.projectPrefix}-AppApiTaskDefinitionArn`,
    });

    new cdk.CfnOutput(this, 'UserQuotasTableName', {
      value: userQuotasTable.tableName,
      description: 'UserQuotas table name',
      exportName: `${config.projectPrefix}-UserQuotasTableName`,
    });

    new cdk.CfnOutput(this, 'QuotaEventsTableName', {
      value: quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      exportName: `${config.projectPrefix}-QuotaEventsTableName`,
    });

    new cdk.CfnOutput(this, 'OidcStateTableName', {
      value: oidcStateTable.tableName,
      description: 'OIDC state table name',
      exportName: `${config.projectPrefix}-OidcStateTableName`,
    });

    new cdk.CfnOutput(this, 'ManagedModelsTableName', {
      value: managedModelsTable.tableName,
      description: 'Managed models table name',
      exportName: `${config.projectPrefix}-ManagedModelsTableName`,
    });

    new cdk.CfnOutput(this, 'SessionsMetadataTableName', {
      value: sessionsMetadataTable.tableName,
      description: 'SessionsMetadata table name for cost tracking',
      exportName: `${config.projectPrefix}-SessionsMetadataTableName`,
    });

    new cdk.CfnOutput(this, 'UserCostSummaryTableName', {
      value: userCostSummaryTable.tableName,
      description: 'UserCostSummary table name for cost aggregation',
      exportName: `${config.projectPrefix}-UserCostSummaryTableName`,
    });

    new cdk.CfnOutput(this, 'SystemCostRollupTableName', {
      value: systemCostRollupTable.tableName,
      description: 'SystemCostRollup table name for admin dashboard',
      exportName: `${config.projectPrefix}-SystemCostRollupTableName`,
    });

    new cdk.CfnOutput(this, 'UsersTableName', {
      value: usersTable.tableName,
      description: 'Users table name for admin user lookup',
      exportName: `${config.projectPrefix}-UsersTableName`,
    });

    new cdk.CfnOutput(this, 'AppRolesTableName', {
      value: appRolesTable.tableName,
      description: 'AppRoles table name for RBAC',
      exportName: `${config.projectPrefix}-AppRolesTableName`,
    });

    new cdk.CfnOutput(this, 'UserFilesBucketName', {
      value: userFilesBucket.bucketName,
      description: 'S3 bucket for user file uploads',
      exportName: `${config.projectPrefix}-UserFilesBucketName`,
    });

    new cdk.CfnOutput(this, 'UserFilesTableName', {
      value: userFilesTable.tableName,
      description: 'DynamoDB table for file metadata',
      exportName: `${config.projectPrefix}-UserFilesTableName`,
    });

    new cdk.CfnOutput(this, 'AssistantsDocumentsIngestionLambdaFunctionArn', {
      value: assistantsDocumentsIngestionlambdaFunction.functionArn,
      description: 'ARN of the Assistants documents ingestion Lambda function',
    });

    new cdk.CfnOutput(this, 'AssistantsVectorStoreBucketName', {
      value: assistantsVectorStoreBucketName,
      description: 'Name of the S3 Vector Bucket for assistants embeddings',
    });

    new cdk.CfnOutput(this, 'AssistantsVectorIndexName', {
      value: assistantsVectorIndexName,
      description: 'Name of the Vector Index within the assistants vector bucket',
    });

    new cdk.CfnOutput(this, 'AssistantsVectorIndexArn', {
      value: assistantsVectorIndex.getAtt('IndexArn').toString(),
      description: 'ARN of the assistants vector index',
    });
  }
}
