/**
 * IAM execution role for the AgentCore Runtime construct.
 *
 * Originally housed Memory / Code Interpreter / Browser roles too,
 * but those were hoisted to `constructs/agentcore/*-construct.ts`
 * refactor. Only the Runtime execution role remains here.
 */

import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { PlatformComputeRefs } from '../platform-compute-refs';

/**
 * Create the AgentCore Runtime execution role with all required
 * policy statements.
 */
export function createRuntimeExecutionRole(
  scope: Construct,
  config: AppConfig,
  refs: PlatformComputeRefs,
): iam.Role {
  // IMPORTANT: keep an explicit, stable roleName. This role's ARN is the
  // AgentCore Runtime `roleArn`; renaming the role (auto-gen) replaces it
  // and risks churning the Runtime on already-deployed stacks. Orphaned-role
  // collisions on a fresh deploy are handled by deleting the orphans.
  const role = new iam.Role(scope, 'AgentCoreRuntimeExecutionRole', {
    roleName: getResourceName(config, 'agentcore-runtime-role'),
    assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
      conditions: {
        StringEquals: { 'aws:SourceAccount': config.awsAccount },
        ArnLike: {
          'aws:SourceArn': `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:*`,
        },
      },
    }),
    description: 'Execution role for AWS Bedrock AgentCore Runtime',
  });

  // ── CloudWatch Logs ──
  role.addToPolicy(new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['logs:DescribeLogStreams', 'logs:CreateLogGroup'],
    resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*`],
  }));
  role.addToPolicy(new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['logs:DescribeLogGroups'],
    resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:*`],
  }));
  role.addToPolicy(new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
    resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*`],
  }));

  // ── X-Ray ──
  role.addToPolicy(new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
    resources: ['*'],
  }));

  // ── CloudWatch Metrics ──
  role.addToPolicy(new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    actions: ['cloudwatch:PutMetricData'],
    resources: ['*'],
    conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
  }));

  // ── Bedrock model invocation ──
  // CountTokens powers per-turn context attribution: Strands' native token
  // counting (use_native_token_count) calls Bedrock's CountTokens API, which
  // is the only way to decompose the otherwise-aggregate inputTokens into
  // system / tools / messages partitions. It acts on the foundation-model
  // resource, already covered below. Without this action a flipped native
  // flag AccessDenies and caches the model into the no-count skip list for
  // the process lifetime.
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'BedrockModelInvocation',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:CountTokens'],
    resources: [`arn:aws:bedrock:*::foundation-model/*`, `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:*`],
  }));

  // ── Bedrock Mantle inference ──
  // Managed models with provider="mantle" run through Bedrock Mantle's
  // OpenAI-compatible endpoint. The agent factory presigns a short-term
  // bearer token with this role (apis/shared/bedrock/bearer_token.py).
  //
  // Mantle has its OWN IAM service namespace — `bedrock-mantle:*`, NOT
  // `bedrock:*`. These statements mirror the AWS managed policy
  // AmazonBedrockMantleInferenceAccess: chat completions need
  // bedrock-mantle:CreateInference (plus Get/List) on the project resource,
  // and the bearer-token transport is authorized separately on `*`.
  // NOTE: this only takes effect once Bedrock Mantle is ENABLED for the
  // account in the target region — IAM alone can't grant a disabled service.
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'BedrockMantleInference',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock-mantle:CreateInference', 'bedrock-mantle:Get*', 'bedrock-mantle:List*'],
    resources: [`arn:aws:bedrock-mantle:*:${config.awsAccount}:project/*`],
  }));
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'BedrockMantleCallWithBearerToken',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock-mantle:CallWithBearerToken'],
    resources: ['*'],
  }));

  // ── AWS Marketplace (model subscription validation) ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'MarketplaceModelAccess',
    effect: iam.Effect.ALLOW,
    actions: ['aws-marketplace:ViewSubscriptions', 'aws-marketplace:Subscribe'],
    resources: ['*'],
  }));

  // ── External MCP Lambda Function URL invocation ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'ExternalMCPLambdaAccess',
    effect: iam.Effect.ALLOW,
    actions: ['lambda:InvokeFunctionUrl', 'lambda:InvokeFunction'],
    resources: [`arn:aws:lambda:${config.awsRegion}:${config.awsAccount}:function:${config.projectPrefix}-mcp-*`],
  }));

  // ── AgentCore Gateway ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'AgentCoreGatewayAccess',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock-agentcore:InvokeGateway', 'bedrock-agentcore:ListGatewayTargets'],
    resources: [`arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:gateway/*`],
  }));

  // ── SSM Parameter Store ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'SSMParameterRead',
    effect: iam.Effect.ALLOW,
    actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
    resources: [`arn:aws:ssm:${config.awsRegion}:${config.awsAccount}:parameter/${config.projectPrefix}/*`],
  }));

  // ── Secrets Manager (OAuth client secrets + auth provider secrets) ──
  const oauthClientSecretsArn = refs.oauthClientSecretsSecret.secretArn;
  const authProviderSecretsArn = refs.authProviderSecretsSecret.secretArn;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'SecretsManagerRead',
    effect: iam.Effect.ALLOW,
    actions: ['secretsmanager:GetSecretValue'],
    resources: [`${oauthClientSecretsArn}*`, `${authProviderSecretsArn}*`],
  }));

  // ── AgentCore Identity OAuth vault secrets ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'AgentCoreIdentityOAuthSecrets',
    effect: iam.Effect.ALLOW,
    actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
    resources: [`arn:aws:secretsmanager:${config.awsRegion}:${config.awsAccount}:secret:bedrock-agentcore-identity!default/oauth2/*`],
  }));

  // ── DynamoDB tables (Users, Roles, OAuth, Quotas, Costs, etc.) ──
  const tableArns = [
    refs.usersTable.tableArn,
    refs.appRolesTable.tableArn,
    refs.oauthProvidersTable.tableArn,
    refs.oauthUserTokensTable.tableArn,
    refs.apiKeysTable.tableArn,
    refs.ragAssistantsTable.tableArn,
    refs.userQuotasTable.tableArn,
    refs.quotaEventsTable.tableArn,
    refs.sessionsMetadataTable.tableArn,
    refs.userCostSummaryTable.tableArn,
    refs.systemCostRollupTable.tableArn,
    refs.managedModelsTable.tableArn,
    refs.authProvidersTable.tableArn,
    refs.fileUploadTable.tableArn,
  ];
  const tableResources = tableArns.flatMap(arn => [arn, `${arn}/index/*`]);
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'DynamoDBTableAccess',
    effect: iam.Effect.ALLOW,
    actions: [
      'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
      'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
      'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
    ],
    resources: tableResources,
  }));

  // ── System prompts table (read-only) ──
  // The inference path resolves the active prompt via system_prompt_resolver
  // and only ever needs GetItem. The table has no GSIs and the inference
  // path never lists or writes prompts — keeping this scope tight prevents
  // a compromised runtime from corrupting the admin-managed catalog.
  // Intentionally NOT folded into the DynamoDBTableAccess bulk grant above.
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'SystemPromptsTableReadAccess',
    effect: iam.Effect.ALLOW,
    actions: ['dynamodb:GetItem'],
    resources: [refs.systemPromptsTable.tableArn],
  }));

  // ── KMS (OAuth token encryption) ──
  const oauthTokenEncryptionKeyArn = refs.oauthTokenEncryptionKey.keyArn;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'KmsOAuthTokenAccess',
    effect: iam.Effect.ALLOW,
    actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey'],
    resources: [oauthTokenEncryptionKeyArn],
  }));

  // ── Cognito (user pool read for token validation) ──
  const cognitoUserPoolId = refs.userPool.userPoolId;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'CognitoUserPoolRead',
    effect: iam.Effect.ALLOW,
    actions: ['cognito-idp:DescribeUserPool', 'cognito-idp:DescribeUserPoolClient'],
    resources: [`arn:aws:cognito-idp:${config.awsRegion}:${config.awsAccount}:userpool/${cognitoUserPoolId}`],
  }));

  // ── File uploads S3 ──
  const userFilesBucketArn = refs.fileUploadBucket.bucketArn;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'UserFilesBucketAccess',
    effect: iam.Effect.ALLOW,
    actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
    resources: [userFilesBucketArn, `${userFilesBucketArn}/*`],
  }));

  // ── Artifacts (S3 write + DDB write) ──
  const artifactsBucketArn = refs.artifactsContentBucket.bucketArn;
  const artifactsTableArn = refs.artifactsTable.tableArn;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'ArtifactsBucketWrite',
    effect: iam.Effect.ALLOW,
    actions: ['s3:PutObject', 's3:PutObjectTagging'],
    resources: [`${artifactsBucketArn}/*`],
  }));
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'ArtifactsTableWrite',
    effect: iam.Effect.ALLOW,
    actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
    resources: [artifactsTableArn, `${artifactsTableArn}/index/*`],
  }));

  // ── Skill reference files (S3, read-only) ──
  // The runtime is a READER of a skill's reference files (PR-6 progressive
  // disclosure); app-api owns writes. Provisioned now so PR-6 needs no infra
  // change. No code consumes it yet.
  const skillResourcesBucketArn = refs.skillResourcesBucket.bucketArn;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'SkillResourcesBucketRead',
    effect: iam.Effect.ALLOW,
    actions: ['s3:GetObject', 's3:ListBucket'],
    resources: [skillResourcesBucketArn, `${skillResourcesBucketArn}/*`],
  }));

  // ── S3 Vectors (RAG query) ──
  const vectorBucketName = refs.ragVectorBucketName;
  const vectorIndexName = refs.ragVectorIndexName;
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'S3VectorsQueryAccess',
    effect: iam.Effect.ALLOW,
    actions: ['s3vectors:GetVector', 's3vectors:GetVectors', 's3vectors:ListVectors',
              's3vectors:QueryVectors', 's3vectors:GetIndex', 's3vectors:ListIndexes'],
    resources: [
      `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}`,
      `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}/index/${vectorIndexName}`,
    ],
  }));

  // ── AgentCore WorkloadIdentity + OAuth token minting ──
  // The agent loop's tool gating in shared/oauth/agentcore_identity.py
  // calls GetResourceOauth2Token to short-circuit to a vaulted token
  // before making an external MCP / connector call. Without it the
  // agent would 503 on any tool that requires a federated token.
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'AgentCoreWorkloadIdentityAccess',
    effect: iam.Effect.ALLOW,
    actions: [
      'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
      'bedrock-agentcore:GetWorkloadIdentity',
      'bedrock-agentcore:GetResourceOauth2Token',
    ],
    resources: [
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*`,
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*/oauth2credentialprovider/*`,
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*`,
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*/workload-identity/*`,
    ],
  }));

  // ── AgentCore Memory ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'AgentCoreMemoryAccess',
    effect: iam.Effect.ALLOW,
    // See app-api-iam-grants.ts for the rationale — these action names
    // mirror the AgentCore Data Plane API. The previous list used
    // speculative names (CreateMemoryEvent, ListMemoryEvents,
    // RetrieveMemory) that don't exist as IAM actions.
    actions: [
      'bedrock-agentcore:CreateEvent',
      'bedrock-agentcore:GetEvent',
      'bedrock-agentcore:ListEvents',
      'bedrock-agentcore:DeleteEvent',
      'bedrock-agentcore:ListActors',
      'bedrock-agentcore:ListSessions',
      'bedrock-agentcore:RetrieveMemoryRecords',
      'bedrock-agentcore:GetMemoryRecord',
      'bedrock-agentcore:ListMemoryRecords',
      'bedrock-agentcore:BatchCreateMemoryRecords',
      'bedrock-agentcore:BatchUpdateMemoryRecords',
      'bedrock-agentcore:BatchDeleteMemoryRecords',
      'bedrock-agentcore:DeleteMemoryRecord',
    ],
    resources: [`arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:memory/*`],
  }));

  // ── AgentCore Code Interpreter + Browser ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'AgentCoreToolsAccess',
    effect: iam.Effect.ALLOW,
    // Real action names per
    // https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrockagentcore.html
    // 'InvokeBrowser' was speculative and silently no-op.
    //
    // Resource ARNs use the *-custom resource types because the
    // platform creates CfnBrowserCustom + CfnCodeInterpreterCustom.
    // The non-custom ARNs (browser/*, code-interpreter/*) refer to
    // AWS-managed resources owned by account 'aws' and would never
    // match the platform's resources — another silent no-op
    // documented in:
    //   https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrockagentcore.html#amazonbedrockagentcore-resources-for-iam-policies
    actions: [
      'bedrock-agentcore:InvokeCodeInterpreter',
      'bedrock-agentcore:StartBrowserSession',
      'bedrock-agentcore:GetBrowserSession',
      'bedrock-agentcore:ListBrowserSessions',
      'bedrock-agentcore:StopBrowserSession',
      'bedrock-agentcore:ConnectBrowserAutomationStream',
      'bedrock-agentcore:ConnectBrowserLiveViewStream',
      'bedrock-agentcore:UpdateBrowserStream',
    ],
    resources: [
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:code-interpreter-custom/*`,
      `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:browser-custom/*`,
    ],
  }));

  // ── ECR pull (for runtime container image) ──
  role.addToPolicy(new iam.PolicyStatement({
    sid: 'ECRPullAccess',
    effect: iam.Effect.ALLOW,
    actions: [
      'ecr:GetDownloadUrlForLayer', 'ecr:BatchGetImage',
      'ecr:GetAuthorizationToken', 'ecr:BatchCheckLayerAvailability',
    ],
    resources: ['*'],
  }));

  return role;
}

/**
 * Create the AgentCore Memory execution role.
 *
 * MOVED to `constructs/agentcore/memory-construct.ts` in Phase 1 of
 * the construct alongside the Memory resource.
 */

/**
 * Create the Code Interpreter execution role.
 *
 * MOVED to `constructs/agentcore/code-interpreter-construct.ts` in
 */

/**
 * Create the Browser execution role.
 *
 * MOVED to `constructs/agentcore/browser-construct.ts` in Phase 1
 */
