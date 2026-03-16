import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as xray from 'aws-cdk-lib/aws-xray';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, getTruncatedResourceName, applyStandardTags } from './config';

export interface InferenceApiStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Inference API Stack - AWS Bedrock AgentCore Shared Resources
 * 
 * This stack creates shared resources used by all AgentCore Runtimes:
 * - AgentCore Memory for conversation context and memory
 * - Code Interpreter Custom for Python code execution
 * - Browser Custom for web browsing capabilities
 * - IAM roles with appropriate permissions
 * 
 * Note: Individual runtimes are created dynamically by Lambda when auth providers are added.
 * Note: ECR repository is created by the build pipeline, not by CDK.
 */
export class InferenceApiStack extends cdk.Stack {
  public readonly memory: bedrock.CfnMemory;
  public readonly codeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly browser: bedrock.CfnBrowserCustom;

  constructor(scope: Construct, id: string, props: InferenceApiStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // Import Image Tag from SSM (set by push-to-ecr.sh)
    // ============================================================
    
    const imageTag = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/inference-api/image-tag`
    );

    // ============================================================
    // ECR Repository Reference
    // ============================================================
    
    // Note: ECR Repository is created automatically by the build pipeline
    // when pushing the first Docker image (see scripts/stack-inference-api/push-to-ecr.sh)
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this,
      'InferenceApiRepository',
      getResourceName(config, 'inference-api')
    );

    const containerImageUri = `${ecrRepository.repositoryUri}:${imageTag}`;

    // ============================================================
    // IAM Execution Role for AgentCore Runtime
    // ============================================================
    
    const runtimeExecutionRole = new iam.Role(this, 'AgentCoreRuntimeExecutionRole', {
      roleName: getResourceName(config, 'agentcore-runtime-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': config.awsAccount,
          },
          ArnLike: {
            'aws:SourceArn': `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:*`,
          },
        },
      }),
      description: 'Execution role for AWS Bedrock AgentCore Runtime',
    });

    // CloudWatch Logs permissions - structured per AWS best practices
    // Log group creation and stream description
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:DescribeLogStreams',
        'logs:CreateLogGroup',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*`],
    }));

    // Describe all log groups (required for runtime initialization)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:DescribeLogGroups',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:*`],
    }));

    // Log stream writing
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`],
    }));

    // X-Ray tracing permissions (full tracing capability)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'xray:PutTraceSegments',
        'xray:PutTelemetryRecords',
        'xray:GetSamplingRules',
        'xray:GetSamplingTargets',
      ],
      resources: ['*'],
    }));

    // CloudWatch Metrics permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'cloudwatch:PutMetricData',
      ],
      resources: ['*'],
      conditions: {
        StringEquals: {
          'cloudwatch:namespace': 'bedrock-agentcore',
        },
      },
    }));

    // Bedrock model invocation permissions (all foundation models + account resources)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockModelInvocation',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: [
        `arn:aws:bedrock:*::foundation-model/*`,
        `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`,
      ],
    }));

    // External MCP Lambda Function URL permissions (for external MCP tools with aws-iam auth)
    // This allows the runtime to invoke Lambda Function URLs that require IAM authentication
    // Scoped to mcp-* functions following the naming convention from mcp-servers repo
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ExternalMCPLambdaAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'lambda:InvokeFunctionUrl',
      ],
      resources: ['*'],
    }));

    // AgentCore Gateway permissions (for MCP tool integration)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AgentCoreGatewayAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:InvokeGateway',
        'bedrock-agentcore:GetGateway',
        'bedrock-agentcore:ListGateways',
      ],
      resources: [`arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:gateway/*`],
    }));

    // SSM Parameter Store read permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'ssm:GetParameter',
        'ssm:GetParameters',
        'ssm:GetParametersByPath',
      ],
      resources: [`arn:aws:ssm:${config.awsRegion}:${config.awsAccount}:parameter/${config.projectPrefix}/*`],
    }));

    // Secrets Manager read permissions for OAuth client secrets (imported from App API Stack)
    const oauthClientSecretsArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/client-secrets-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OAuthClientSecretsAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'secretsmanager:GetSecretValue',
        'secretsmanager:DescribeSecret',
      ],
      resources: [
        oauthClientSecretsArn,
        `${oauthClientSecretsArn}*`, // Include wildcard for random suffix
      ],
    }));

    // DynamoDB Users Table permissions (imported from App API Stack)
    const usersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/users/users-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'UsersTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        usersTableArn,
        `${usersTableArn}/index/*`, // GSI permissions
      ],
    }));

    // DynamoDB AppRoles Table permissions (imported from App API Stack)
    // This table stores both RBAC roles AND tool catalog definitions
    const appRolesTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rbac/app-roles-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AppRolesTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:Scan',
        // Note: No write permissions - inference API only reads tool definitions and roles
      ],
      resources: [
        appRolesTableArn,
        `${appRolesTableArn}/index/*`, // GSI permissions
      ],
    }));

    // DynamoDB OAuth Providers Table permissions (imported from App API Stack)
    const oauthProvidersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/providers-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OAuthProvidersTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:Scan',
        // Note: No write permissions - inference API only reads OAuth provider configs
      ],
      resources: [
        oauthProvidersTableArn,
        `${oauthProvidersTableArn}/index/*`, // GSI permissions
      ],
    }));

    // DynamoDB OAuth User Tokens Table permissions (imported from App API Stack)
    const oauthUserTokensTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/user-tokens-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OAuthUserTokensTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
        // Note: Inference API needs write access to store/update OAuth tokens
      ],
      resources: [
        oauthUserTokensTableArn,
        `${oauthUserTokensTableArn}/index/*`, // GSI permissions
      ],
    }));

    // KMS Key permissions for OAuth token encryption/decryption
    const oauthTokenEncryptionKeyArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/oauth/token-encryption-key-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OAuthTokenEncryptionKeyAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'kms:Decrypt',
        'kms:Encrypt',
        'kms:GenerateDataKey',
        'kms:DescribeKey',
      ],
      resources: [oauthTokenEncryptionKeyArn],
    }));

    // DynamoDB API Keys Table permissions (imported from Infrastructure Stack)
    const apiKeysTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/api-keys-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ApiKeysTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        apiKeysTableArn,
        `${apiKeysTableArn}/index/*`, // GSI permissions (KeyHashIndex)
      ],
    }));

    // DynamoDB Assistants Table permissions (imported from RagIngestionStack)
    const assistantsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag/assistants-table-arn`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AssistantsTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        assistantsTableArn,
        `${assistantsTableArn}/index/*`, // GSI permissions
      ],
    }));

    // S3 Assistants Documents Bucket permissions - NOT NEEDED by inference API
    // Documents are only accessed during ingestion (Lambda function)
    // Inference API only queries the vector store, not the raw documents

    // DynamoDB User Files Table permissions (constructed from naming convention)
    const userFilesTableName = getResourceName(config, 'user-files');
    const userFilesTableArn = `arn:aws:dynamodb:${config.awsRegion}:${config.awsAccount}:table/${userFilesTableName}`;
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'UserFilesTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        // Note: Inference API only reads file metadata - App API handles uploads
      ],
      resources: [
        userFilesTableArn,
        `${userFilesTableArn}/index/*`, // GSI permissions
      ],
    }));

    // S3 User Files Bucket permissions (constructed from naming convention)
    const userFilesBucketName = getResourceName(config, 'user-files', config.awsAccount);
    const userFilesBucketArn = `arn:aws:s3:::${userFilesBucketName}`;
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'UserFilesBucketAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject',
        's3:GetObjectVersion',
        // Note: Inference API only reads uploaded files - App API handles uploads
      ],
      resources: [
        `${userFilesBucketArn}/*`,
      ],
    }));

    // S3 Vectors permissions for RAG (READ-ONLY for queries)
    const assistantsVectorBucketName = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/rag/vector-bucket-name`
    );
    
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AssistantsVectorStoreAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        's3vectors:GetVector',
        's3vectors:GetVectors',
        's3vectors:QueryVectors',  // Main action for RAG search
        's3vectors:GetIndex',
        's3vectors:ListIndexes',
        // Note: No PutVectors or DeleteVector - inference API only reads
      ],
      resources: [
        `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${assistantsVectorBucketName}`,
        `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${assistantsVectorBucketName}/index/*`,
      ],
    }));

    // Bedrock permissions for generating query embeddings
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockEmbeddingsAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
      ],
      resources: [
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.titan-embed-text-v2*`,
      ],
    }));

    // ECR image access - scoped to specific repository
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRImageAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:BatchGetImage',
        'ecr:GetDownloadUrlForLayer',
        'ecr:BatchCheckLayerAvailability',
      ],
      resources: [ecrRepository.repositoryArn],
    }));

    // ECR token access - required for authentication (must be wildcard)
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRTokenAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'ecr:GetAuthorizationToken',
      ],
      resources: ['*'],
    }));

    // Bedrock AgentCore workload identity and access token permissions
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'GetAgentAccessToken',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:GetWorkloadAccessToken',
        'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
        'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/default`,
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/default/workload-identity/hosted_agent_*`,
      ],
    }));

    // DynamoDB Quota Tables permissions (imported from App API Stack)
    const userQuotasTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/user-quotas-table-arn`
    );
    const quotaEventsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/quota/quota-events-table-arn`
    );

    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'QuotaTablesAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        userQuotasTableArn,
        `${userQuotasTableArn}/index/*`,
        quotaEventsTableArn,
        `${quotaEventsTableArn}/index/*`,
      ],
    }));

    // DynamoDB Cost Tracking Tables permissions (imported from App API Stack)
    const sessionsMetadataTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-arn`
    );
    const userCostSummaryTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-arn`
    );
    const systemCostRollupTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-arn`
    );

    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CostTrackingTablesAccess',
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
        sessionsMetadataTableArn,
        `${sessionsMetadataTableArn}/index/*`,
        userCostSummaryTableArn,
        `${userCostSummaryTableArn}/index/*`,
        systemCostRollupTableArn,
        `${systemCostRollupTableArn}/index/*`,
      ],
    }));

    // DynamoDB Managed Models Table permissions (imported from App API Stack)
    const managedModelsTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/admin/managed-models-table-arn`
    );

    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ManagedModelsTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        managedModelsTableArn,
        `${managedModelsTableArn}/index/*`,
      ],
    }));

    // DynamoDB Auth Providers Table permissions (imported from App API Stack)
    const authProvidersTableArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-providers-table-arn`
    );

    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AuthProvidersTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [
        authProvidersTableArn,
        `${authProvidersTableArn}/index/*`,
      ],
    }));

    // Secrets Manager permissions for auth provider client secrets
    const authProviderSecretsArn = ssm.StringParameter.valueForStringParameter(
      this,
      `/${config.projectPrefix}/auth/auth-provider-secrets-arn`
    );

    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AuthProviderSecretsAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'secretsmanager:GetSecretValue',
        'secretsmanager:DescribeSecret',
      ],
      resources: [
        authProviderSecretsArn,
        `${authProviderSecretsArn}*`,
      ],
    }));

    // ============================================================
    // IAM Execution Role for AgentCore Memory
    // ============================================================
    
    const memoryExecutionRole = new iam.Role(this, 'AgentCoreMemoryExecutionRole', {
      roleName: getResourceName(config, 'agentcore-memory-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Memory',
    });

    // Bedrock model access for memory processing
    memoryExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
      ],
      resources: [
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/anthropic.claude-*`,
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.nova-*`,
      ],
    }));

    // ============================================================
    // IAM Execution Role for Code Interpreter
    // ============================================================
    
    const codeInterpreterExecutionRole = new iam.Role(this, 'CodeInterpreterExecutionRole', {
      roleName: getResourceName(config, 'code-interpreter-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Code Interpreter',
    });

    // CloudWatch Logs permissions
    codeInterpreterExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/code-interpreter/*`],
    }));

    // ============================================================
    // IAM Execution Role for Browser
    // ============================================================
    
    const browserExecutionRole = new iam.Role(this, 'BrowserExecutionRole', {
      roleName: getResourceName(config, 'browser-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AWS Bedrock AgentCore Browser',
    });

    // CloudWatch Logs permissions
    browserExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/browser/*`],
    }));

    // ============================================================
    // AgentCore Memory
    // ============================================================
    
    this.memory = new bedrock.CfnMemory(this, 'AgentCoreMemory', {
      name: getResourceName(config, 'agentcore_memory').replace(/-/g, '_'),
      eventExpiryDuration: 90, // 90 days (property expects days, not hours; max is 365, min is 7)
      memoryExecutionRoleArn: memoryExecutionRole.roleArn,
      description: 'AgentCore Memory for maintaining conversation context, user preferences, and semantic facts',
      memoryStrategies: [
        {
          semanticMemoryStrategy: {
            name: 'SemanticFactExtraction',
            description: 'Extracts and stores semantic facts from conversations',
          },
        },
        {
          summaryMemoryStrategy: {
            name: 'ConversationSummary',
            description: 'Generates and stores conversation summaries',
          },
        },
        {
          userPreferenceMemoryStrategy: {
            name: 'UserPreferenceExtraction',
            description: 'Identifies and stores user preferences',
          },
        },
      ],
    });

    // ============================================================
    // AgentCore Code Interpreter Custom
    // ============================================================
    
    this.codeInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'CodeInterpreterCustom', {
      name: getResourceName(config, 'code_interpreter').replace(/-/g, '_'),
      description: 'Custom Code Interpreter for Python code execution with advanced configuration',
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      executionRoleArn: codeInterpreterExecutionRole.roleArn,
    });

    this.codeInterpreter.node.addDependency(codeInterpreterExecutionRole);

    // ============================================================
    // AgentCore Browser Custom
    // ============================================================
    
    this.browser = new bedrock.CfnBrowserCustom(this, 'BrowserCustom', {
      name: getResourceName(config, 'browser').replace(/-/g, '_'),
      description: 'Custom Browser for secure web interaction and data extraction',
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      executionRoleArn: browserExecutionRole.roleArn,
    });

    this.browser.node.addDependency(browserExecutionRole);

    // ============================================================
    // AgentCore Runtime
    // ============================================================
    
    // Grant Runtime permission to access Memory
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'MemoryAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        // Memory configuration
        'bedrock-agentcore:GetMemory',
        'bedrock-agentcore:GetMemoryStrategies',
        // Event operations (create only - runtime doesn't delete)
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        // Memory retrieval
        'bedrock-agentcore:RetrieveMemory',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:ListMemoryRecords',
        // Session operations (read only - runtime doesn't delete sessions)
        'bedrock-agentcore:ListMemorySessions',
        'bedrock-agentcore:GetMemorySession',
      ],
      resources: [this.memory.attrMemoryArn],
    }));

    // Grant Runtime permission to use Code Interpreter
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CodeInterpreterAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:CreateCodeInterpreterSession',
      ],
      resources: [this.codeInterpreter.attrCodeInterpreterArn],
    }));

    // Grant Runtime permission to use Browser
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrowserAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:InvokeBrowser',
      ],
      resources: [this.browser.attrBrowserArn],
    }));

    // ============================================================
    // Observability: CloudWatch Log Group for Runtime
    // ============================================================

    const runtimeLogGroup = new logs.LogGroup(this, 'AgentCoreRuntimeLogGroup', {
      logGroupName: `/aws/bedrock-agentcore/runtimes/${config.projectPrefix}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // NOTE: X-Ray TransactionSearchConfig is an account-level singleton.
    // It cannot be created via CloudFormation if it already exists.
    // Manage it via AWS CLI instead:
    //   aws xray update-transaction-search-config --indexing-percentage <5|100>

    // ============================================================
    // Observability: Vended Log Deliveries for AgentCore Resources
    // ============================================================
    // Uses CloudWatch Logs vended logs API (CfnDeliverySource/Destination/Delivery)
    // to configure APPLICATION_LOGS and TRACES for CDK-managed resources.
    // Runtime log deliveries are configured in the runtime-provisioner Lambda
    // since runtimes are created dynamically per auth provider.

    // --- Memory: APPLICATION_LOGS ---
    const memoryLogsLogGroup = new logs.LogGroup(this, 'MemoryLogsLogGroup', {
      logGroupName: `/aws/vendedlogs/bedrock-agentcore/memory/${config.projectPrefix}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const memoryLogsSource = new logs.CfnDeliverySource(this, 'MemoryLogsSource', {
      name: `${config.projectPrefix}-memory-logs`,
      logType: 'APPLICATION_LOGS',
      resourceArn: this.memory.attrMemoryArn,
    });
    memoryLogsSource.node.addDependency(this.memory);

    const memoryLogsDestination = new logs.CfnDeliveryDestination(this, 'MemoryLogsDestination', {
      name: `${config.projectPrefix}-memory-logs-dest`,
      deliveryDestinationType: 'CWL',
      destinationResourceArn: memoryLogsLogGroup.logGroupArn,
    });

    const memoryLogsDelivery = new logs.CfnDelivery(this, 'MemoryLogsDelivery', {
      deliverySourceName: memoryLogsSource.name,
      deliveryDestinationArn: memoryLogsDestination.attrArn,
    });
    memoryLogsDelivery.node.addDependency(memoryLogsSource);
    memoryLogsDelivery.node.addDependency(memoryLogsDestination);

    // --- Memory: TRACES ---
    const memoryTracesSource = new logs.CfnDeliverySource(this, 'MemoryTracesSource', {
      name: `${config.projectPrefix}-memory-traces`,
      logType: 'TRACES',
      resourceArn: this.memory.attrMemoryArn,
    });
    memoryTracesSource.node.addDependency(this.memory);

    const memoryTracesDestination = new logs.CfnDeliveryDestination(this, 'MemoryTracesDestination', {
      name: `${config.projectPrefix}-memory-traces-dest`,
      deliveryDestinationType: 'XRAY',
    });

    const memoryTracesDelivery = new logs.CfnDelivery(this, 'MemoryTracesDelivery', {
      deliverySourceName: memoryTracesSource.name,
      deliveryDestinationArn: memoryTracesDestination.attrArn,
    });
    memoryTracesDelivery.node.addDependency(memoryTracesSource);
    memoryTracesDelivery.node.addDependency(memoryTracesDestination);

    // NOTE: Code Interpreter and Browser do NOT need vended log delivery right now.
    // Valid resource types are: code-interpreter, memory, workload-identity,
    // code-interpreter-custom, runtime, gateway.

    // ============================================================
    // Observability: X-Ray Sampling Rule for AgentCore
    // ============================================================

    new xray.CfnSamplingRule(this, 'AgentCoreSamplingRule', {
      samplingRule: {
        ruleName: getTruncatedResourceName(config, 32, 'ac-sampling'),
        priority: 100,
        fixedRate: config.production ? 0.05 : 1.0,
        reservoirSize: config.production ? 5 : 50,
        serviceName: '*',
        serviceType: '*',
        host: '*',
        httpMethod: '*',
        urlPath: '/invocations',
        resourceArn: '*',
        version: 1,
      },
    });

    // ============================================================
    // Observability: X-Ray Group for AgentCore Traces
    // ============================================================

    new xray.CfnGroup(this, 'AgentCoreXRayGroup', {
      groupName: getTruncatedResourceName(config, 32, 'ac-traces'),
      filterExpression: 'annotation.gen_ai_system = "strands-agents" OR service(id(name: "bedrock-agentcore", type: "AWS::BedrockAgentCore"))',
      insightsConfiguration: {
        insightsEnabled: true,
        notificationsEnabled: config.production,
      },
    });

    // ============================================================
    // Observability: CloudWatch Dashboard
    // ============================================================

    const dashboard = new cloudwatch.Dashboard(this, 'AgentCoreObservabilityDashboard', {
      dashboardName: getResourceName(config, 'agentcore-observability'),
      defaultInterval: cdk.Duration.hours(3),
    });

    const agentCoreNamespace = 'bedrock-agentcore';

    const invocationCountMetric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InvocationCount',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const invocationErrorMetric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InvocationErrors',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const latencyP50Metric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InvocationLatency',
      statistic: 'p50',
      period: cdk.Duration.minutes(5),
    });

    const latencyP90Metric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InvocationLatency',
      statistic: 'p90',
      period: cdk.Duration.minutes(5),
    });

    const latencyP99Metric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InvocationLatency',
      statistic: 'p99',
      period: cdk.Duration.minutes(5),
    });

    const inputTokensMetric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'InputTokens',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    const outputTokensMetric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'OutputTokens',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# AgentCore Runtime Observability\n**Project:** ${config.projectPrefix} | **Region:** ${config.awsRegion}`,
        width: 24,
        height: 1,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Invocation Count & Errors',
        left: [invocationCountMetric],
        right: [invocationErrorMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Invocation Latency (p50 / p90 / p99)',
        left: [latencyP50Metric, latencyP90Metric, latencyP99Metric],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Token Usage (Input / Output)',
        left: [inputTokensMetric, outputTokensMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.LogQueryWidget({
        title: 'Recent Runtime Errors',
        logGroupNames: [runtimeLogGroup.logGroupName],
        queryLines: [
          'fields @timestamp, @message',
          'filter @message like /(?i)error|exception|traceback/',
          'sort @timestamp desc',
          'limit 20',
        ],
        width: 12,
        height: 6,
      }),
    );

    // ============================================================
    // Observability: CloudWatch Alarms
    // ============================================================

    new cloudwatch.Alarm(this, 'AgentCoreHighErrorRateAlarm', {
      alarmName: getResourceName(config, 'agentcore-high-error-rate'),
      alarmDescription: 'AgentCore Runtime invocation error rate exceeded threshold',
      metric: invocationErrorMetric,
      threshold: config.production ? 10 : 50,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, 'AgentCoreHighLatencyAlarm', {
      alarmName: getResourceName(config, 'agentcore-high-latency'),
      alarmDescription: 'AgentCore Runtime p99 latency exceeded threshold',
      metric: latencyP99Metric,
      threshold: 30000, // 30 seconds
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // SSM Parameters for Cross-Stack References
    // ============================================================
    
    // Export runtime execution role ARN for Lambda-created runtimes
    new ssm.StringParameter(this, 'RuntimeExecutionRoleArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/runtime-execution-role-arn`,
      stringValue: runtimeExecutionRole.roleArn,
      description: 'Runtime execution role ARN for Lambda-created AgentCore Runtimes',
      tier: ssm.ParameterTier.STANDARD,
    });
    
    new ssm.StringParameter(this, 'InferenceApiMemoryArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/memory-arn`,
      stringValue: this.memory.attrMemoryArn,
      description: 'Inference API AgentCore Memory ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiMemoryIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/memory-id`,
      stringValue: this.memory.attrMemoryId,
      description: 'Inference API AgentCore Memory ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiCodeInterpreterIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/code-interpreter-id`,
      stringValue: this.codeInterpreter.attrCodeInterpreterId,
      description: 'Inference API AgentCore Code Interpreter ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiCodeInterpreterArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/code-interpreter-arn`,
      stringValue: this.codeInterpreter.attrCodeInterpreterArn,
      description: 'Inference API AgentCore Code Interpreter ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiBrowserIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/browser-id`,
      stringValue: this.browser.attrBrowserId,
      description: 'Inference API AgentCore Browser ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'InferenceApiBrowserArnParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/browser-arn`,
      stringValue: this.browser.attrBrowserArn,
      description: 'Inference API AgentCore Browser ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export ECR repository URI for Lambda-created runtimes
    new ssm.StringParameter(this, 'EcrRepositoryUriParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/ecr-repository-uri`,
      stringValue: ecrRepository.repositoryUri,
      description: 'Inference API ECR Repository URI for runtime container images',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export observability log group name
    new ssm.StringParameter(this, 'RuntimeLogGroupNameParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/runtime-log-group-name`,
      stringValue: runtimeLogGroup.logGroupName,
      description: 'CloudWatch Log Group name for AgentCore Runtime observability',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    

    new cdk.CfnOutput(this, 'InferenceApiMemoryArn', {
      value: this.memory.attrMemoryArn,
      description: 'Inference API AgentCore Memory ARN',
      exportName: `${config.projectPrefix}-InferenceApiMemoryArn`,
    });

    new cdk.CfnOutput(this, 'InferenceApiMemoryId', {
      value: this.memory.attrMemoryId,
      description: 'Inference API AgentCore Memory ID',
      exportName: `${config.projectPrefix}-InferenceApiMemoryId`,
    });

    new cdk.CfnOutput(this, 'InferenceApiCodeInterpreterId', {
      value: this.codeInterpreter.attrCodeInterpreterId,
      description: 'Inference API AgentCore Code Interpreter ID',
      exportName: `${config.projectPrefix}-InferenceApiCodeInterpreterId`,
    });

    new cdk.CfnOutput(this, 'InferenceApiBrowserId', {
      value: this.browser.attrBrowserId,
      description: 'Inference API AgentCore Browser ID',
      exportName: `${config.projectPrefix}-InferenceApiBrowserId`,
    });

    new cdk.CfnOutput(this, 'EcrRepositoryUri', {
      value: ecrRepository.repositoryUri,
      description: 'Inference API ECR Repository URI',
      exportName: `${config.projectPrefix}-InferenceApiEcrRepositoryUri`,
    });

    new cdk.CfnOutput(this, 'ObservabilityDashboardName', {
      value: dashboard.dashboardName,
      description: 'CloudWatch Dashboard for AgentCore observability',
      exportName: `${config.projectPrefix}-AgentCoreObservabilityDashboard`,
    });

    new cdk.CfnOutput(this, 'RuntimeLogGroupName', {
      value: runtimeLogGroup.logGroupName,
      description: 'CloudWatch Log Group for AgentCore Runtime',
      exportName: `${config.projectPrefix}-AgentCoreRuntimeLogGroup`,
    });
   }
}
