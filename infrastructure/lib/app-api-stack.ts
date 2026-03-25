import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as logs from "aws-cdk-lib/aws-logs";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as sns from "aws-cdk-lib/aws-sns";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import { Construct } from "constructs";
import { AppConfig, getResourceName, applyStandardTags, getRemovalPolicy } from "./config";

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
    const vpcId = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/vpc-id`);
    const vpcCidr = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/vpc-cidr`);
    const privateSubnetIdsString = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/private-subnet-ids`);
    const availabilityZonesString = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/availability-zones`);

    // Import image tag from SSM (set by push-to-ecr.sh)
    const imageTag = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/app-api/image-tag`);

    const vpc = ec2.Vpc.fromVpcAttributes(this, "ImportedVpc", {
      vpcId: vpcId,
      vpcCidrBlock: vpcCidr,
      availabilityZones: cdk.Fn.split(",", availabilityZonesString),
      privateSubnetIds: cdk.Fn.split(",", privateSubnetIdsString),
    });

    // Import ALB Security Group
    const albSecurityGroupId = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/alb-security-group-id`);
    const albSecurityGroup = ec2.SecurityGroup.fromSecurityGroupId(this, "ImportedAlbSecurityGroup", albSecurityGroupId);

    // Import ALB
    const albArn = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/alb-arn`);
    const alb = elbv2.ApplicationLoadBalancer.fromApplicationLoadBalancerAttributes(this, "ImportedAlb", {
      loadBalancerArn: albArn,
      securityGroupId: albSecurityGroupId,
    });

    // Import ALB Listener
    const albListenerArn = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/alb-listener-arn`);
    const albListener = elbv2.ApplicationListener.fromApplicationListenerAttributes(this, "ImportedAlbListener", {
      listenerArn: albListenerArn,
      securityGroup: albSecurityGroup,
    });

    // Import ECS Cluster
    const ecsClusterName = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/ecs-cluster-name`);
    const ecsClusterArn = ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/network/ecs-cluster-arn`);
    const ecsCluster = ecs.Cluster.fromClusterAttributes(this, "ImportedEcsCluster", {
      clusterName: ecsClusterName,
      clusterArn: ecsClusterArn,
      vpc: vpc,
      securityGroups: [],
    });

    // ============================================================
    // Security Groups
    // ============================================================

    // ECS Task Security Group - Allow traffic from ALB
    const ecsSecurityGroup = new ec2.SecurityGroup(this, "AppEcsSecurityGroup", {
      vpc: vpc,
      securityGroupName: getResourceName(config, "app-ecs-sg"),
      description: "Security group for App API ECS Fargate tasks",
      allowAllOutbound: true,
    });

    ecsSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(8000), "Allow traffic from ALB to App API tasks");

    // ============================================================
    // Assistants Table
    // Base Table PK (String) SK (String)
    // Owner Status Index GSI_PK (String) GSI_SK (String)
    // Visibility Status Index GSI2_PK (String) GSI2_SK (String)
    // ============================================================
    const assistantsTable = new dynamodb.Table(this, "AssistantsTable", {
      tableName: getResourceName(config, "assistants"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    assistantsTable.addGlobalSecondaryIndex({
      indexName: "OwnerStatusIndex",
      partitionKey: {
        name: "GSI_PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI_SK",
        type: dynamodb.AttributeType.STRING,
      },
    });

    assistantsTable.addGlobalSecondaryIndex({
      indexName: "VisibilityStatusIndex",
      partitionKey: {
        name: "GSI2_PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI2_SK",
        type: dynamodb.AttributeType.STRING,
      },
    });

    assistantsTable.addGlobalSecondaryIndex({
      indexName: "SharedWithIndex",
      partitionKey: {
        name: "GSI3_PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI3_SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Note: RAG resources (documents bucket, vector bucket, vector index, ingestion Lambda)
    // are created in RagIngestionStack and imported via SSM parameters.







    // ============================================================
    // Import Core Tables from Infrastructure Stack
    // ============================================================
    // OAuth, RBAC, and Users tables are created in Infrastructure Stack
    // to avoid circular dependencies. Import their ARNs/names via SSM.

    const oidcStateTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/oidc-state-table-name`
    );
    const oidcStateTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/oidc-state-table-arn`
    );

    const usersTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/users/users-table-name`
    );
    const usersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/users/users-table-arn`
    );

    const appRolesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rbac/app-roles-table-name`
    );
    const appRolesTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rbac/app-roles-table-arn`
    );

    // API Keys table (imported from Infrastructure Stack)
    const apiKeysTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/api-keys-table-name`
    );
    const apiKeysTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/api-keys-table-arn`
    );

    const oauthProvidersTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/providers-table-name`
    );
    const oauthProvidersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/providers-table-arn`
    );

    const oauthUserTokensTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/user-tokens-table-name`
    );
    const oauthUserTokensTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/user-tokens-table-arn`
    );

    const oauthTokenEncryptionKeyArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/token-encryption-key-arn`
    );

    const oauthClientSecretsArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/client-secrets-arn`
    );

    // Import shared tables from Infrastructure Stack
    // These tables were moved from AppApiStack to InfrastructureStack to eliminate circular dependencies
    const userQuotasTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/user-quotas-table-name`
    );
    const userQuotasTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/user-quotas-table-arn`
    );

    const quotaEventsTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/quota-events-table-name`
    );
    const quotaEventsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/quota-events-table-arn`
    );

    const sessionsMetadataTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-name`
    );
    const sessionsMetadataTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-arn`
    );

    const userCostSummaryTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-name`
    );
    const userCostSummaryTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-arn`
    );

    const systemCostRollupTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-name`
    );
    const systemCostRollupTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-arn`
    );

    const managedModelsTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/admin/managed-models-table-name`
    );
    const managedModelsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/admin/managed-models-table-arn`
    );

    const userSettingsTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/settings/user-settings-table-name`
    );
    const userSettingsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/settings/user-settings-table-arn`
    );

    const authProvidersTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-providers-table-name`
    );
    const authProvidersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-providers-table-arn`
    );
    const authProvidersStreamArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-providers-stream-arn`
    );

    const authProviderSecretsArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-provider-secrets-arn`
    );

    // ============================================================
    // File Upload Storage (imported from Infrastructure Stack)
    // ============================================================
    // These resources were moved to InfrastructureStack to avoid a circular
    // dependency: InferenceApiStack (tier 2) needs these ARNs but deploys
    // before AppApiStack (tier 3).

    const userFilesBucketName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/user-file-uploads/bucket-name`
    );
    const userFilesBucketArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/user-file-uploads/bucket-arn`
    );
    const userFilesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/user-file-uploads/table-name`
    );
    const userFilesTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/user-file-uploads/table-arn`
    );

    // ============================================================
    // ECS Task Definition
    // ============================================================
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-app-api/push-to-ecr.sh)
    const taskDefinition = new ecs.FargateTaskDefinition(this, "AppApiTaskDefinition", {
      family: getResourceName(config, "app-api-task"),
      cpu: config.appApi.cpu,
      memoryLimitMiB: config.appApi.memory,
    });

    // Create log group for ECS task
    const logGroup = new logs.LogGroup(this, "AppApiLogGroup", {
      logGroupName: `/ecs/${config.projectPrefix}/app-api`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Reference the ECR repository created by the build pipeline
    const ecrRepository = ecr.Repository.fromRepositoryName(this, "AppApiRepository", getResourceName(config, "app-api"));

    // Container Definition
    const container = taskDefinition.addContainer("AppApiContainer", {
      containerName: "app-api",
      image: ecs.ContainerImage.fromEcrRepository(ecrRepository, imageTag),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "app-api",
        logGroup: logGroup,
      }),
      environment: {
        AWS_REGION: config.awsRegion,
        PROJECT_PREFIX: config.projectPrefix,
        FRONTEND_URL: config.domainName ? `https://${config.domainName}` : 'http://localhost:4200',
        DYNAMODB_QUOTA_TABLE: userQuotasTableName,
        DYNAMODB_EVENTS_TABLE: quotaEventsTableName,
        DYNAMODB_OIDC_STATE_TABLE_NAME: oidcStateTableName,
        DYNAMODB_MANAGED_MODELS_TABLE_NAME: managedModelsTableName,
        DYNAMODB_SESSIONS_METADATA_TABLE_NAME: sessionsMetadataTableName,
        DYNAMODB_COST_SUMMARY_TABLE_NAME: userCostSummaryTableName,
        DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: systemCostRollupTableName,
        DYNAMODB_USERS_TABLE_NAME: usersTableName,
        DYNAMODB_APP_ROLES_TABLE_NAME: appRolesTableName,
        DYNAMODB_USER_FILES_TABLE_NAME: userFilesTableName,
        S3_USER_FILES_BUCKET_NAME: userFilesBucketName,
        FILE_UPLOAD_MAX_SIZE_BYTES: String(config.fileUpload?.maxFileSizeBytes || 4194304),
        FILE_UPLOAD_MAX_FILES_PER_MESSAGE: String(config.fileUpload?.maxFilesPerMessage || 5),
        FILE_UPLOAD_USER_QUOTA_BYTES: String(config.fileUpload?.userQuotaBytes || 1073741824),
        // RAG resources - imported from RagIngestionStack via SSM
        S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/rag/documents-bucket-name`
        ),
        DYNAMODB_ASSISTANTS_TABLE_NAME: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/rag/assistants-table-name`
        ),
        S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/rag/vector-bucket-name`
        ),
        S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/rag/vector-index-name`
        ),
        // AgentCore Memory Configuration (imported from InferenceApiStack)
        AGENTCORE_MEMORY_TYPE: 'dynamodb',
        AGENTCORE_MEMORY_ID: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/inference-api/memory-id`
        ),
        DYNAMODB_API_KEYS_TABLE_NAME: apiKeysTableName,
        OAUTH_TOKEN_ENCRYPTION_KEY_ARN: oauthTokenEncryptionKeyArn,
        OAUTH_CLIENT_SECRETS_ARN: oauthClientSecretsArn,
        DYNAMODB_AUTH_PROVIDERS_TABLE_NAME: authProvidersTableName,
        AUTH_PROVIDER_SECRETS_ARN: authProviderSecretsArn,
        DYNAMODB_USER_SETTINGS_TABLE_NAME: userSettingsTableName,
        SHARED_CONVERSATIONS_TABLE_NAME: ssm.StringParameter.valueForStringParameter(
          this,
          `/${config.projectPrefix}/shares/shared-conversations-table-name`
        ),
      },
      portMappings: [
        {
          containerPort: 8000,
          protocol: ecs.Protocol.TCP,
        },
      ],
      healthCheck: {
        command: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // Grant permissions for database access
    // DynamoDB permissions are granted via IAM policy below

    // Grant permissions for assistants base table (local to this stack)
    assistantsTable.grantReadWriteData(taskDefinition.taskRole);

    // Grant permissions for user settings table (imported from InfrastructureStack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UserSettingsTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          userSettingsTableArn,
          `${userSettingsTableArn}/index/*`,
        ],
      })
    );

    // Grant explicit permissions for GSI queries (grantReadWriteData doesn't include GSI Query permissions)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:Query',
          'dynamodb:Scan'
        ],
        resources: [
          `${assistantsTable.tableArn}/index/*`
        ],
      })
    );

    // Grant permissions for RAG assistants table (imported from RagIngestionStack)
    const ragAssistantsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag/assistants-table-arn`
    );
    
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'RagAssistantsTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          ragAssistantsTableArn,
          `${ragAssistantsTableArn}/index/*`,
        ],
      })
    );

    // Grant permissions for RAG documents bucket (imported from RagIngestionStack)
    const ragDocumentsBucketArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag/documents-bucket-arn`
    );
    
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'RagDocumentsBucketAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:ListBucket',
        ],
        resources: [
          ragDocumentsBucketArn,
          `${ragDocumentsBucketArn}/*`,
        ],
      })
    );

    // Grant S3 Vectors permissions for RAG vector store (imported from RagIngestionStack)
    // Note: The vector bucket/index are created in RagIngestionStack and imported via SSM.
    // The App API uses these for document vector operations (query, delete, list).
    const ragVectorBucketName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag/vector-bucket-name`
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'RagVectorStoreAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          "s3vectors:ListVectorBuckets",
          "s3vectors:GetVectorBucket",
          "s3vectors:GetIndex",
          "s3vectors:PutVectors",
          "s3vectors:ListVectors",
          "s3vectors:ListIndexes",
          "s3vectors:GetVector",
          "s3vectors:GetVectors",
          "s3vectors:DeleteVector",
          "s3vectors:QueryVectors",
        ],
        resources: [
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${ragVectorBucketName}`,
          `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${ragVectorBucketName}/index/*`,
        ],
      })
    );

    // Grant Bedrock permissions for Titan embeddings (used for RAG query embeddings)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockTitanEmbeddings',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.titan-embed-text-v2:0`,
        ],
      })
    );

    // Grant Bedrock permissions to list foundation models
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockListModels',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:ListFoundationModels'],
        resources: ['*'],
      })
    );


    // Grant permissions for quota management tables (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UserQuotasTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          userQuotasTableArn,
          `${userQuotasTableArn}/index/*`,
        ],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'QuotaEventsTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          quotaEventsTableArn,
          `${quotaEventsTableArn}/index/*`,
        ],
      })
    );

    // Grant permissions for OIDC state table (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'OidcStateTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [oidcStateTableArn, `${oidcStateTableArn}/index/*`],
      })
    );

    // Grant permissions for managed models table (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'ManagedModelsTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          managedModelsTableArn,
          `${managedModelsTableArn}/index/*`,
        ],
      })
    );

    // Grant permissions for cost tracking tables (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'SessionsMetadataTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          sessionsMetadataTableArn,
          `${sessionsMetadataTableArn}/index/*`,
        ],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UserCostSummaryTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          userCostSummaryTableArn,
          `${userCostSummaryTableArn}/index/*`,
        ],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'SystemCostRollupTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          systemCostRollupTableArn,
          `${systemCostRollupTableArn}/index/*`,
        ],
      })
    );

    // Grant permissions for users table (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UsersTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [usersTableArn, `${usersTableArn}/index/*`],
      })
    );

    // Grant permissions for AppRoles table (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AppRolesTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchWriteItem',
        ],
        resources: [appRolesTableArn, `${appRolesTableArn}/index/*`],
      })
    );

    // Grant permissions for file upload resources (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesTableReadWrite',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchWriteItem',
          'dynamodb:BatchGetItem',
        ],
        resources: [userFilesTableArn, `${userFilesTableArn}/index/*`],
      })
    );
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UserFilesBucketReadWrite',
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject',
          's3:GetObjectVersion',
          's3:ListBucket',
        ],
        resources: [userFilesBucketArn, `${userFilesBucketArn}/*`],
      })
    );

    // Grant Bedrock permissions for title generation (Nova Micro)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockTitleGeneration',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          // Nova Micro foundation model - allow in all regions for flexibility
          `arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0`,
          // Cross-region inference profile (us. prefix works from any region)
          `arn:aws:bedrock:*:${config.awsAccount}:inference-profile/us.amazon.nova-micro-v1:0`,
        ],
      }),
    );

    // Grant Bedrock permissions for API-key converse endpoint (/chat/api-converse)
    // This allows the App API to call Bedrock Converse directly for API key users,
    // matching the same permissions the AgentCore Runtime execution role has.
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockApiKeyConverse',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${config.awsAccount}:inference-profile/*`,
        ],
      }),
    );

    // Grant permissions for OAuth provider management (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'OAuthProvidersTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [oauthProvidersTableArn, `${oauthProvidersTableArn}/index/*`],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'OAuthUserTokensTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [oauthUserTokensTableArn, `${oauthUserTokensTableArn}/index/*`],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'OAuthTokenEncryptionKeyAccess',
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: [oauthTokenEncryptionKeyArn],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'OAuthClientSecretsAccess',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [`${oauthClientSecretsArn}*`], // Wildcard for random suffix
      })
    );
    // Grant permissions for API Keys table (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'ApiKeysTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
        ],
        resources: [apiKeysTableArn, `${apiKeysTableArn}/index/*`],
      })
    );

    // Grant permissions for auth provider management (imported from Infrastructure Stack)
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AuthProvidersTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          authProvidersTableArn,
          `${authProvidersTableArn}/index/*`,
        ],
      })
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AuthProviderSecretsAccess',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [`${authProviderSecretsArn}*`], // Wildcard for random suffix
      })
    );

    // Grant SSM read permissions for runtime image tag
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'SsmParameterReadAccess',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter', 'ssm:GetParameters'],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${config.projectPrefix}/inference-api/image-tag`,
        ],
      })
    );

    // Grant permissions for shared conversations table (imported from Infrastructure Stack)
    const sharedConversationsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/shares/shared-conversations-table-arn`
    );

    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'SharedConversationsTableAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
        ],
        resources: [
          sharedConversationsTableArn,
          `${sharedConversationsTableArn}/index/*`,
        ],
      })
    );

    // ============================================================
    // Fine-Tuning Resources (optional - from SageMakerFineTuningStack)
    // ============================================================

    // Default: fine-tuning disabled
    container.addEnvironment('FINE_TUNING_ENABLED', 'false');

    if (config.fineTuning.enabled) {
      // Import resource identifiers from SageMakerFineTuningStack via SSM
      const ftJobsTableName = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/jobs-table-name`
      );
      const ftJobsTableArn = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/jobs-table-arn`
      );
      const ftAccessTableName = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/access-table-name`
      );
      const ftAccessTableArn = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/access-table-arn`
      );
      const ftDataBucketName = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/data-bucket-name`
      );
      const ftDataBucketArn = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/data-bucket-arn`
      );
      const sagemakerRoleArn = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/sagemaker-execution-role-arn`
      );
      const sagemakerSgId = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/sagemaker-security-group-id`
      );
      const ftPrivateSubnetIds = ssm.StringParameter.valueForStringParameter(
        this, `/${config.projectPrefix}/fine-tuning/private-subnet-ids`
      );

      // Add fine-tuning environment variables to container
      container.addEnvironment('FINE_TUNING_ENABLED', 'true');
      container.addEnvironment('DYNAMODB_FINE_TUNING_JOBS_TABLE_NAME', ftJobsTableName);
      container.addEnvironment('DYNAMODB_FINE_TUNING_ACCESS_TABLE_NAME', ftAccessTableName);
      container.addEnvironment('S3_FINE_TUNING_BUCKET_NAME', ftDataBucketName);
      container.addEnvironment('SAGEMAKER_EXECUTION_ROLE_ARN', sagemakerRoleArn);
      container.addEnvironment('SAGEMAKER_SECURITY_GROUP_ID', sagemakerSgId);
      container.addEnvironment('SAGEMAKER_SUBNET_IDS', ftPrivateSubnetIds);

      // Grant ECS task role: DynamoDB access to fine-tuning tables
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'FineTuningJobsTableAccess',
          effect: iam.Effect.ALLOW,
          actions: [
            'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
            'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
          ],
          resources: [ftJobsTableArn, `${ftJobsTableArn}/index/*`],
        })
      );

      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'FineTuningAccessTableAccess',
          effect: iam.Effect.ALLOW,
          actions: [
            'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
            'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
          ],
          resources: [ftAccessTableArn, `${ftAccessTableArn}/index/*`],
        })
      );

      // Grant ECS task role: S3 access to fine-tuning data bucket (for presigned URLs)
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'FineTuningDataBucketAccess',
          effect: iam.Effect.ALLOW,
          actions: [
            's3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket',
          ],
          resources: [ftDataBucketArn, `${ftDataBucketArn}/*`],
        })
      );

      // Grant ECS task role: SageMaker job management
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'SageMakerTrainingJobManagement',
          effect: iam.Effect.ALLOW,
          actions: [
            'sagemaker:CreateTrainingJob',
            'sagemaker:DescribeTrainingJob',
            'sagemaker:StopTrainingJob',
            'sagemaker:CreateTransformJob',
            'sagemaker:DescribeTransformJob',
            'sagemaker:StopTransformJob',
            'sagemaker:CreateModel',
            'sagemaker:DeleteModel',
          ],
          resources: [
            `arn:aws:sagemaker:${config.awsRegion}:${config.awsAccount}:training-job/${config.projectPrefix}-ft-*`,
            `arn:aws:sagemaker:${config.awsRegion}:${config.awsAccount}:transform-job/${config.projectPrefix}-inf-*`,
            `arn:aws:sagemaker:${config.awsRegion}:${config.awsAccount}:model/model-${config.projectPrefix}-inf-*`,
          ],
        })
      );

      // Grant iam:PassRole on the SageMaker execution role
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'SageMakerPassRole',
          effect: iam.Effect.ALLOW,
          actions: ['iam:PassRole'],
          resources: [sagemakerRoleArn],
          conditions: {
            StringEquals: {
              'iam:PassedToService': 'sagemaker.amazonaws.com',
            },
          },
        })
      );

      // Grant CloudWatch Logs read access for training job logs
      taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'SageMakerLogsReadAccess',
          effect: iam.Effect.ALLOW,
          actions: ['logs:DescribeLogStreams', 'logs:GetLogEvents', 'logs:FilterLogEvents'],
          resources: [
            `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/sagemaker/*`,
          ],
        })
      );
    }

    // ============================================================
    // Runtime Provisioner Lambda
    // ============================================================

    // Reconstruct AuthProviders table reference for DynamoDB Stream event source
    // Note: fromTableAttributes accepts either tableName OR tableArn, not both
    const authProvidersTable = dynamodb.Table.fromTableAttributes(this, 'ImportedAuthProvidersTable', {
      tableArn: authProvidersTableArn,
      tableStreamArn: authProvidersStreamArn,
    });

    // Create Lambda function for runtime provisioning
    const runtimeProvisionerFunction = new lambda.Function(this, "RuntimeProvisionerFunction", {
      functionName: getResourceName(config, "runtime-provisioner"),
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: "lambda_function.lambda_handler",
      code: lambda.Code.fromAsset("../backend/lambda-functions/runtime-provisioner"),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      architecture: lambda.Architecture.ARM_64,
      environment: {
        PROJECT_PREFIX: config.projectPrefix,
        AUTH_PROVIDERS_TABLE: authProvidersTableName,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // Grant DynamoDB Stream read permissions
    authProvidersTable.grantStreamRead(runtimeProvisionerFunction);

    // Grant DynamoDB UpdateItem permissions for Auth Providers table
    authProvidersTable.grantReadWriteData(runtimeProvisionerFunction);

    // Grant Bedrock AgentCore permissions
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "BedrockAgentCoreRuntimeManagement",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:CreateAgentRuntime",
          "bedrock-agentcore:CreateAgentRuntimeEndpoint",
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
          "bedrock-agentcore:UpdateAgentRuntime",
          "bedrock-agentcore:DeleteAgentRuntime",
          "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:ListAgentRuntimeEndpoints",
          "bedrock-agentcore:AllowVendedLogDeliveryForResource",
        ],
        resources: ["*"], // Runtime ARNs are not known at deployment time
      })
    );

    // Grant permission to create service-linked roles for Bedrock AgentCore
    // Required on first CreateAgentRuntime call in an account
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CreateNetworkServiceLinkedRole",
        effect: iam.Effect.ALLOW,
        actions: ["iam:CreateServiceLinkedRole"],
        resources: ["arn:aws:iam::*:role/aws-service-role/network.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreNetwork"],
        conditions: {
          StringLike: {
            "iam:AWSServiceName": "network.bedrock-agentcore.amazonaws.com",
          },
        },
      })
    );

    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CreateIdentityServiceLinkedRole",
        effect: iam.Effect.ALLOW,
        actions: ["iam:CreateServiceLinkedRole"],
        resources: ["arn:aws:iam::*:role/aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreRuntimeIdentity"],
        conditions: {
          StringEquals: {
            "iam:AWSServiceName": "runtime-identity.bedrock-agentcore.amazonaws.com",
          },
        },
      })
    );

    // Grant SSM Parameter Store read/write permissions
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SSMParameterAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:PutParameter",
          "ssm:DeleteParameter",
        ],
        resources: [
          `arn:aws:ssm:${config.awsRegion}:${config.awsAccount}:parameter/${config.projectPrefix}/*`,
        ],
      })
    );

    // Grant ECR read permissions
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ECRReadAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "ecr:DescribeRepositories",
          "ecr:DescribeImages",
          "ecr:GetAuthorizationToken",
        ],
        resources: ["*"], // ECR authorization token requires wildcard
      })
    );

    // Grant CloudWatch Logs delivery permissions for runtime observability
    // The Lambda sets up vended log deliveries (APPLICATION_LOGS + TRACES) after creating runtimes
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsDeliveryManagement",
        effect: iam.Effect.ALLOW,
        actions: [
          "logs:PutDeliverySource",
          "logs:PutDeliveryDestination",
          "logs:CreateDelivery",
          "logs:DeleteDeliverySource",
          "logs:DeleteDeliveryDestination",
          "logs:DeleteDelivery",
          "logs:GetDelivery",
          "logs:GetDeliverySource",
          "logs:GetDeliveryDestination",
          "logs:DescribeDeliveries",
          "logs:DescribeDeliverySources",
          "logs:DescribeDeliveryDestinations",
          "logs:CreateLogGroup",
          "logs:DescribeLogGroups",
        ],
        resources: ["*"],
      })
    );

    // Grant X-Ray resource policy permissions for TRACES delivery destinations
    // Required so the Lambda can create X-Ray delivery destinations and auto-create
    // the X-Ray resource policy that allows delivery.logs.amazonaws.com to write traces
    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "XRayResourcePolicyManagement",
        effect: iam.Effect.ALLOW,
        actions: [
          "xray:PutResourcePolicy",
          "xray:ListResourcePolicies",
          "xray:GetTraceSegmentDestination",
        ],
        resources: ["*"],
      })
    );

    // Grant IAM PassRole permission for runtime execution role
    const runtimeExecutionRoleArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/inference-api/runtime-execution-role-arn`
    );

    runtimeProvisionerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "IAMPassRoleForRuntime",
        effect: iam.Effect.ALLOW,
        actions: ["iam:PassRole"],
        resources: [runtimeExecutionRoleArn],
        conditions: {
          StringEquals: {
            "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
          },
        },
      })
    );

    // Add DynamoDB Stream event source
    runtimeProvisionerFunction.addEventSource(
      new lambdaEventSources.DynamoEventSource(authProvidersTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 1,
        retryAttempts: 3,
        bisectBatchOnError: true,
      })
    );

    // Store Lambda function ARN in SSM
    new ssm.StringParameter(this, "RuntimeProvisionerFunctionArnParameter", {
      parameterName: `/${config.projectPrefix}/lambda/runtime-provisioner-arn`,
      stringValue: runtimeProvisionerFunction.functionArn,
      description: "Runtime Provisioner Lambda function ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Runtime Updater Lambda
    // ============================================================

    // Create SNS topic for runtime update alerts
    const runtimeUpdateAlertsTopic = new sns.Topic(this, "RuntimeUpdateAlertsTopic", {
      topicName: getResourceName(config, "runtime-update-alerts"),
      displayName: "AgentCore Runtime Update Alerts",
    });

    // Create Lambda function for runtime updates
    const runtimeUpdaterFunction = new lambda.Function(this, "RuntimeUpdaterFunction", {
      functionName: getResourceName(config, "runtime-updater"),
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: "lambda_function.lambda_handler",
      code: lambda.Code.fromAsset("../backend/lambda-functions/runtime-updater"),
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      architecture: lambda.Architecture.ARM_64,
      environment: {
        PROJECT_PREFIX: config.projectPrefix,
        AUTH_PROVIDERS_TABLE: authProvidersTableName,
        SNS_TOPIC_ARN: runtimeUpdateAlertsTopic.topicArn,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // Grant Bedrock AgentCore permissions
    runtimeUpdaterFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "BedrockAgentCoreRuntimeUpdates",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:UpdateAgentRuntime",
        ],
        resources: ["*"], // Runtime ARNs are not known at deployment time
      })
    );

    // Grant IAM PassRole permission for runtime execution role
    runtimeUpdaterFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "IAMPassRoleForRuntime",
        effect: iam.Effect.ALLOW,
        actions: ["iam:PassRole"],
        resources: [runtimeExecutionRoleArn],
        conditions: {
          StringEquals: {
            "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
          },
        },
      })
    );

    // Grant DynamoDB Scan and UpdateItem permissions
    authProvidersTable.grantReadWriteData(runtimeUpdaterFunction);

    // Grant SSM Parameter Store read permissions
    runtimeUpdaterFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SSMParameterReadAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ],
        resources: [
          `arn:aws:ssm:${config.awsRegion}:${config.awsAccount}:parameter/${config.projectPrefix}/*`,
        ],
      })
    );

    // Grant ECR read permissions
    runtimeUpdaterFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ECRReadAccessForUpdater",
        effect: iam.Effect.ALLOW,
        actions: [
          "ecr:DescribeRepositories",
          "ecr:DescribeImages",
          "ecr:GetAuthorizationToken",
        ],
        resources: ["*"], // ECR authorization token requires wildcard
      })
    );

    // Grant SNS Publish permissions
    runtimeUpdateAlertsTopic.grantPublish(runtimeUpdaterFunction);

    // Create EventBridge rule to detect SSM parameter changes
    const imageTagChangeRule = new events.Rule(this, "ImageTagChangeRule", {
      ruleName: getResourceName(config, "image-tag-change"),
      description: "Triggers Runtime Updater when inference API image tag changes",
      eventPattern: {
        source: ["aws.ssm"],
        detailType: ["Parameter Store Change"],
        detail: {
          name: [`/${config.projectPrefix}/inference-api/image-tag`],
          operation: ["Update"],
        },
      },
    });

    // Add Lambda as target for EventBridge rule
    imageTagChangeRule.addTarget(new targets.LambdaFunction(runtimeUpdaterFunction));

    // Store Lambda function ARN in SSM
    new ssm.StringParameter(this, "RuntimeUpdaterFunctionArnParameter", {
      parameterName: `/${config.projectPrefix}/lambda/runtime-updater-arn`,
      stringValue: runtimeUpdaterFunction.functionArn,
      description: "Runtime Updater Lambda function ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // Store SNS topic ARN in SSM
    new ssm.StringParameter(this, "RuntimeUpdateAlertsTopicArnParameter", {
      parameterName: `/${config.projectPrefix}/sns/runtime-update-alerts-arn`,
      stringValue: runtimeUpdateAlertsTopic.topicArn,
      description: "SNS topic ARN for runtime update alerts",
      tier: ssm.ParameterTier.STANDARD,
    });

    // Grant permissions for AgentCore Memory (imported from InferenceApiStack)
    const memoryArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/inference-api/memory-arn`
    );
    
    taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AgentCoreMemoryAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          // Memory configuration
          'bedrock-agentcore:GetMemory',
          'bedrock-agentcore:GetMemoryStrategies',
          // Event operations
          'bedrock-agentcore:CreateEvent',
          'bedrock-agentcore:DeleteEvent',
          'bedrock-agentcore:ListEvents',
          // Memory retrieval
          'bedrock-agentcore:RetrieveMemory',
          'bedrock-agentcore:RetrieveMemoryRecords',
          'bedrock-agentcore:ListMemoryRecords',
          // Memory record deletion
          'bedrock-agentcore:BatchDeleteMemoryRecords',
          // Session operations
          'bedrock-agentcore:ListMemorySessions',
          'bedrock-agentcore:GetMemorySession',
          'bedrock-agentcore:DeleteMemorySession',
        ],
        resources: [memoryArn],
      })
    );

    // ============================================================
    // Target Group
    // ============================================================
    const targetGroup = new elbv2.ApplicationTargetGroup(this, "AppApiTargetGroup", {
      vpc: vpc,
      targetGroupName: getResourceName(config, "app-api-tg"),
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        enabled: true,
        path: "/health",
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        healthyHttpCodes: "200",
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // Add listener rule for App API (all traffic)
    // Since this is the only target, route all HTTPS traffic to this target group
    albListener.addTargetGroups("AppApiTargetGroupAttachment", {
      targetGroups: [targetGroup],
      priority: 1,
      conditions: [elbv2.ListenerCondition.pathPatterns(["/*"])],
    });

    // ============================================================
    // ECS Fargate Service
    // ============================================================
    this.ecsService = new ecs.FargateService(this, "AppApiService", {
      cluster: ecsCluster,
      serviceName: getResourceName(config, "app-api-service"),
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

    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    scaling.scaleOnMemoryUtilization("MemoryScaling", {
      targetUtilizationPercent: 80,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    new cdk.CfnOutput(this, "EcsClusterName", {
      value: ecsClusterName,
      description: "ECS Cluster Name",
      exportName: `${config.projectPrefix}-AppEcsClusterName`,
    });

    new cdk.CfnOutput(this, "EcsServiceName", {
      value: this.ecsService.serviceName,
      description: "ECS Service Name",
      exportName: `${config.projectPrefix}-AppEcsServiceName`,
    });

    new cdk.CfnOutput(this, "TaskDefinitionArn", {
      value: taskDefinition.taskDefinitionArn,
      description: "Task Definition ARN",
      exportName: `${config.projectPrefix}-AppApiTaskDefinitionArn`,
    });

    new cdk.CfnOutput(this, "UserQuotasTableName", {
      value: userQuotasTableName,
      description: "UserQuotas table name",
      exportName: `${config.projectPrefix}-UserQuotasTableName`,
    });

    new cdk.CfnOutput(this, "QuotaEventsTableName", {
      value: quotaEventsTableName,
      description: "QuotaEvents table name",
      exportName: `${config.projectPrefix}-QuotaEventsTableName`,
    });

    new cdk.CfnOutput(this, "OidcStateTableName", {
      value: oidcStateTableName,
      description: "OIDC state table name (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-OidcStateTableName`,
    });

    new cdk.CfnOutput(this, "ManagedModelsTableName", {
      value: managedModelsTableName,
      description: "Managed models table name",
      exportName: `${config.projectPrefix}-ManagedModelsTableName`,
    });

    new cdk.CfnOutput(this, "SessionsMetadataTableName", {
      value: sessionsMetadataTableName,
      description: "SessionsMetadata table name for cost tracking",
      exportName: `${config.projectPrefix}-SessionsMetadataTableName`,
    });

    new cdk.CfnOutput(this, "UserCostSummaryTableName", {
      value: userCostSummaryTableName,
      description: "UserCostSummary table name for cost aggregation",
      exportName: `${config.projectPrefix}-UserCostSummaryTableName`,
    });

    new cdk.CfnOutput(this, "SystemCostRollupTableName", {
      value: systemCostRollupTableName,
      description: "SystemCostRollup table name for admin dashboard",
      exportName: `${config.projectPrefix}-SystemCostRollupTableName`,
    });

    new cdk.CfnOutput(this, "UsersTableName", {
      value: usersTableName,
      description: "Users table name for admin user lookup (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-UsersTableName`,
    });

    new cdk.CfnOutput(this, "AppRolesTableName", {
      value: appRolesTableName,
      description: "AppRoles table name for RBAC (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-AppRolesTableName`,
    });

    new cdk.CfnOutput(this, "UserFilesBucketName", {
      value: userFilesBucketName,
      description: "S3 bucket for user file uploads (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-UserFilesBucketName`,
    });

    new cdk.CfnOutput(this, "UserFilesTableName", {
      value: userFilesTableName,
      description: "DynamoDB table for file metadata (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-UserFilesTableName`,
    });

    // Note: Lambda function output is now in RagIngestionStack

    new cdk.CfnOutput(this, "AuthProvidersTableName", {
      value: authProvidersTableName,
      description: "Auth providers configuration table name",
      exportName: `${config.projectPrefix}-AuthProvidersTableName`,
    });

    new cdk.CfnOutput(this, "AuthProviderSecretsSecretArn", {
      value: authProviderSecretsArn,
      description: "Secrets Manager ARN for auth provider client secrets",
      exportName: `${config.projectPrefix}-AuthProviderSecretsSecretArn`,
    });

    new cdk.CfnOutput(this, "OAuthProvidersTableName", {
      value: oauthProvidersTableName,
      description: "OAuth providers configuration table name (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-OAuthProvidersTableName`,
    });

    new cdk.CfnOutput(this, "OAuthUserTokensTableName", {
      value: oauthUserTokensTableName,
      description: "OAuth user tokens table name (KMS encrypted, imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-OAuthUserTokensTableName`,
    });

    new cdk.CfnOutput(this, "OAuthTokenEncryptionKeyArn", {
      value: oauthTokenEncryptionKeyArn,
      description: "KMS key ARN for OAuth token encryption (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-OAuthTokenEncryptionKeyArn`,
    });

    new cdk.CfnOutput(this, "OAuthClientSecretsSecretArn", {
      value: oauthClientSecretsArn,
      description: "Secrets Manager ARN for OAuth client secrets (imported from Infrastructure Stack)",
      exportName: `${config.projectPrefix}-OAuthClientSecretsSecretArn`,
    });

    new cdk.CfnOutput(this, "RuntimeProvisionerFunctionArn", {
      value: runtimeProvisionerFunction.functionArn,
      description: "Runtime Provisioner Lambda function ARN",
      exportName: `${config.projectPrefix}-RuntimeProvisionerFunctionArn`,
    });

    new cdk.CfnOutput(this, "RuntimeUpdaterFunctionArn", {
      value: runtimeUpdaterFunction.functionArn,
      description: "Runtime Updater Lambda function ARN",
      exportName: `${config.projectPrefix}-RuntimeUpdaterFunctionArn`,
    });

    new cdk.CfnOutput(this, "RuntimeUpdateAlertsTopicArn", {
      value: runtimeUpdateAlertsTopic.topicArn,
      description: "SNS topic ARN for runtime update alerts",
      exportName: `${config.projectPrefix}-RuntimeUpdateAlertsTopicArn`,
    });   
  }
}
