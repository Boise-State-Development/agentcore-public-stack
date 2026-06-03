/**
 * SSM parameter resolution + container environment builder for App API.
 *
 * Extracts the ~130 lines of ssm.StringParameter.valueForStringParameter
 * calls and the ~120 lines of container environment entries into two
 * focused functions. The main construct calls these and gets back typed
 * objects it can pass to the task definition and IAM grants module.
 */

import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, buildCorsOrigins } from '../../config';
import { PlatformComputeRefs } from '../platform-compute-refs';

/** All SSM-resolved values the App API construct needs. */
export interface AppApiSsmParams {
  // Network
  vpcId: string;
  vpcCidr: string;
  privateSubnetIds: string;
  availabilityZones: string;
  albSecurityGroupId: string;
  albArn: string;
  albListenerArn: string;
  ecsClusterName: string;
  ecsClusterArn: string;
  // Tables (names + ARNs)
  oidcStateTableName: string;
  oidcStateTableArn: string;
  usersTableName: string;
  usersTableArn: string;
  appRolesTableName: string;
  appRolesTableArn: string;
  apiKeysTableName: string;
  apiKeysTableArn: string;
  oauthProvidersTableName: string;
  oauthProvidersTableArn: string;
  oauthUserTokensTableName: string;
  oauthUserTokensTableArn: string;
  oauthTokenEncryptionKeyArn: string;
  oauthClientSecretsArn: string;
  userQuotasTableName: string;
  userQuotasTableArn: string;
  quotaEventsTableName: string;
  quotaEventsTableArn: string;
  sessionsMetadataTableName: string;
  sessionsMetadataTableArn: string;
  userCostSummaryTableName: string;
  userCostSummaryTableArn: string;
  systemCostRollupTableName: string;
  systemCostRollupTableArn: string;
  managedModelsTableName: string;
  managedModelsTableArn: string;
  userSettingsTableName: string;
  userSettingsTableArn: string;
  userMenuLinksTableName: string;
  userMenuLinksTableArn: string;
  systemPromptsTableName: string;
  systemPromptsTableArn: string;
  authProvidersTableName: string;
  authProvidersTableArn: string;
  authProviderSecretsArn: string;
  // Cognito
  cognitoUserPoolArn: string;
  cognitoUserPoolId: string;
  cognitoAppClientId: string;
  cognitoIssuerUrl: string;
  cognitoDomainUrl: string;
  // BFF
  bffSessionsTableName: string;
  bffSessionsTableArn: string;
  bffCookieSigningKeyArn: string;
  bffCookieDataKeySecretArn: string;
  cognitoBFFAppClientId: string;
  cognitoBFFAppClientSecretArn: string;
  // Voice
  voiceTicketReplayTableName: string;
  voiceTicketReplayTableArn: string;
  voiceTicketSigningSecretArn: string;
  // Inference
  inferenceApiRuntimeEndpointUrl: string;
  // File uploads
  userFilesBucketName: string;
  userFilesBucketArn: string;
  userFilesTableName: string;
  userFilesTableArn: string;
  // RAG
  ragDocumentsBucketName: string;
  ragAssistantsTableName: string;
  ragVectorBucketName: string;
  ragVectorIndexName: string;
  ragAssistantsTableArn: string;
  ragDocumentsBucketArn: string;
  sharedConversationsTableName: string;
  sharedConversationsTableArn: string;
  memoryId: string;
  // Workload identity
  workloadIdentityName: string;
}

/**
 * Same-stack values that App API needs from sibling PlatformStack
 * constructs. Passed in directly rather than read from SSM because
 * `valueForStringParameter` would deadlock on first deploy: CFN
 * resolves SSM template parameters before any of the stack's
 * resources are created, so reading a parameter that this same
 * stack publishes is unsatisfiable.
 */
export interface AppApiBackendOverrides {
  /** AgentCore Memory ID (from InferenceAgentCoreConstruct.memory.attrMemoryId). */
  memoryId: string;
  /** AgentCore Runtime endpoint URL (from InferenceAgentCoreConstruct.runtimeEndpointUrl). */
  inferenceApiRuntimeEndpointUrl: string;
}

/** Resolve every value the App API construct needs.
 *
 * Sourced from typed PlatformStack refs (see PlatformComputeRefs),
 * NOT from SSM. Reading SSM via `valueForStringParameter` from
 * within the same stack that publishes the parameter dead-locks
 * on first deploy — see the file-level docstring for the full
 * explanation.
 */
export function resolveAppApiParams(
  refs: PlatformComputeRefs,
  overrides: AppApiBackendOverrides,
): AppApiSsmParams {
  return {
    // Network
    vpcId: refs.vpc.vpcId,
    vpcCidr: refs.vpc.vpcCidrBlock,
    privateSubnetIds: refs.vpc.privateSubnets.map((s) => s.subnetId).join(','),
    availabilityZones: refs.vpc.availabilityZones.join(','),
    albSecurityGroupId: refs.albSecurityGroup.securityGroupId,
    albArn: refs.alb.loadBalancerArn,
    albListenerArn: refs.albListener.listenerArn,
    ecsClusterName: refs.ecsCluster.clusterName,
    ecsClusterArn: refs.ecsCluster.clusterArn,
    // Tables
    oidcStateTableName: refs.oidcStateTable.tableName,
    oidcStateTableArn: refs.oidcStateTable.tableArn,
    usersTableName: refs.usersTable.tableName,
    usersTableArn: refs.usersTable.tableArn,
    appRolesTableName: refs.appRolesTable.tableName,
    appRolesTableArn: refs.appRolesTable.tableArn,
    apiKeysTableName: refs.apiKeysTable.tableName,
    apiKeysTableArn: refs.apiKeysTable.tableArn,
    oauthProvidersTableName: refs.oauthProvidersTable.tableName,
    oauthProvidersTableArn: refs.oauthProvidersTable.tableArn,
    oauthUserTokensTableName: refs.oauthUserTokensTable.tableName,
    oauthUserTokensTableArn: refs.oauthUserTokensTable.tableArn,
    oauthTokenEncryptionKeyArn: refs.oauthTokenEncryptionKey.keyArn,
    oauthClientSecretsArn: refs.oauthClientSecretsSecret.secretArn,
    userQuotasTableName: refs.userQuotasTable.tableName,
    userQuotasTableArn: refs.userQuotasTable.tableArn,
    quotaEventsTableName: refs.quotaEventsTable.tableName,
    quotaEventsTableArn: refs.quotaEventsTable.tableArn,
    sessionsMetadataTableName: refs.sessionsMetadataTable.tableName,
    sessionsMetadataTableArn: refs.sessionsMetadataTable.tableArn,
    userCostSummaryTableName: refs.userCostSummaryTable.tableName,
    userCostSummaryTableArn: refs.userCostSummaryTable.tableArn,
    systemCostRollupTableName: refs.systemCostRollupTable.tableName,
    systemCostRollupTableArn: refs.systemCostRollupTable.tableArn,
    managedModelsTableName: refs.managedModelsTable.tableName,
    managedModelsTableArn: refs.managedModelsTable.tableArn,
    userSettingsTableName: refs.userSettingsTable.tableName,
    userSettingsTableArn: refs.userSettingsTable.tableArn,
    userMenuLinksTableName: refs.userMenuLinksTable.tableName,
    userMenuLinksTableArn: refs.userMenuLinksTable.tableArn,
    systemPromptsTableName: refs.systemPromptsTable.tableName,
    systemPromptsTableArn: refs.systemPromptsTable.tableArn,
    authProvidersTableName: refs.authProvidersTable.tableName,
    authProvidersTableArn: refs.authProvidersTable.tableArn,
    authProviderSecretsArn: refs.authProviderSecretsSecret.secretArn,
    // Cognito
    cognitoUserPoolArn: refs.userPool.userPoolArn,
    cognitoUserPoolId: refs.userPool.userPoolId,
    cognitoAppClientId: refs.bffAppClient.userPoolClientId,
    cognitoIssuerUrl: refs.cognitoIssuerUrl,
    cognitoDomainUrl: refs.cognitoDomainUrl,
    // BFF
    bffSessionsTableName: refs.bffSessionsTable.tableName,
    bffSessionsTableArn: refs.bffSessionsTable.tableArn,
    bffCookieSigningKeyArn: refs.bffCookieSigningKey.keyArn,
    bffCookieDataKeySecretArn: refs.bffCookieDataKeySecret.secretArn,
    cognitoBFFAppClientId: refs.bffAppClient.userPoolClientId,
    cognitoBFFAppClientSecretArn: refs.bffAppClientSecret.secretArn,
    // Voice
    voiceTicketReplayTableName: refs.voiceTicketReplayTable.tableName,
    voiceTicketReplayTableArn: refs.voiceTicketReplayTable.tableArn,
    voiceTicketSigningSecretArn: refs.voiceTicketSigningSecret.secretArn,
    // Inference
    inferenceApiRuntimeEndpointUrl: overrides.inferenceApiRuntimeEndpointUrl,
    // File uploads
    userFilesBucketName: refs.fileUploadBucket.bucketName,
    userFilesBucketArn: refs.fileUploadBucket.bucketArn,
    userFilesTableName: refs.fileUploadTable.tableName,
    userFilesTableArn: refs.fileUploadTable.tableArn,
    // RAG
    ragDocumentsBucketName: refs.ragDocumentsBucket.bucketName,
    ragAssistantsTableName: refs.ragAssistantsTable.tableName,
    ragVectorBucketName: refs.ragVectorBucketName,
    ragVectorIndexName: refs.ragVectorIndexName,
    ragAssistantsTableArn: refs.ragAssistantsTable.tableArn,
    ragDocumentsBucketArn: refs.ragDocumentsBucket.bucketArn,
    sharedConversationsTableName: refs.sharedConversationsTable.tableName,
    sharedConversationsTableArn: refs.sharedConversationsTable.tableArn,
    memoryId: overrides.memoryId,
    // Workload identity
    workloadIdentityName: refs.platformWorkloadIdentity.name,
  };
}

/** Build the container environment map for the App API task. */
export function buildAppApiEnvironment(
  config: AppConfig,
  params: AppApiSsmParams,
): Record<string, string> {
  return {
    AWS_REGION: config.awsRegion,
    PROJECT_PREFIX: config.projectPrefix,
    FRONTEND_URL: config.domainName ? `https://${config.domainName}` : 'http://localhost:4200',
    CORS_ORIGINS: buildCorsOrigins(config, config.appApi.additionalCorsOrigins).join(','),
    AGENTCORE_LOCAL_OAUTH_CALLBACK_URL: config.domainName
      ? `https://${config.domainName}/oauth-complete`
      : 'http://localhost:4200/oauth-complete',
    DYNAMODB_QUOTA_TABLE: params.userQuotasTableName,
    DYNAMODB_QUOTA_EVENTS_TABLE: params.quotaEventsTableName,
    DYNAMODB_OIDC_STATE_TABLE_NAME: params.oidcStateTableName,
    DYNAMODB_MANAGED_MODELS_TABLE_NAME: params.managedModelsTableName,
    DYNAMODB_SESSIONS_METADATA_TABLE_NAME: params.sessionsMetadataTableName,
    DYNAMODB_COST_SUMMARY_TABLE_NAME: params.userCostSummaryTableName,
    DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: params.systemCostRollupTableName,
    DYNAMODB_USERS_TABLE_NAME: params.usersTableName,
    DYNAMODB_APP_ROLES_TABLE_NAME: params.appRolesTableName,
    DYNAMODB_USER_FILES_TABLE_NAME: params.userFilesTableName,
    S3_USER_FILES_BUCKET_NAME: params.userFilesBucketName,
    FILE_UPLOAD_MAX_SIZE_BYTES: String(4194304),
    FILE_UPLOAD_MAX_FILES_PER_MESSAGE: String(5),
    FILE_UPLOAD_USER_QUOTA_BYTES: String(1073741824),
    S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: params.ragDocumentsBucketName,
    DYNAMODB_ASSISTANTS_TABLE_NAME: params.ragAssistantsTableName,
    S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME: params.ragVectorBucketName,
    S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME: params.ragVectorIndexName,
    AGENTCORE_MEMORY_TYPE: 'dynamodb',
    AGENTCORE_MEMORY_ID: params.memoryId,
    DYNAMODB_API_KEYS_TABLE_NAME: params.apiKeysTableName,
    OAUTH_TOKEN_ENCRYPTION_KEY_ARN: params.oauthTokenEncryptionKeyArn,
    OAUTH_CLIENT_SECRETS_ARN: params.oauthClientSecretsArn,
    DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: params.oauthProvidersTableName,
    DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME: params.oauthUserTokensTableName,
    AGENTCORE_RUNTIME_WORKLOAD_NAME: params.workloadIdentityName,
    DYNAMODB_AUTH_PROVIDERS_TABLE_NAME: params.authProvidersTableName,
    AUTH_PROVIDER_SECRETS_ARN: params.authProviderSecretsArn,
    DYNAMODB_USER_SETTINGS_TABLE_NAME: params.userSettingsTableName,
    DYNAMODB_USER_MENU_LINKS_TABLE_NAME: params.userMenuLinksTableName,
    DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME: params.systemPromptsTableName,
    COGNITO_USER_POOL_ID: params.cognitoUserPoolId,
    COGNITO_APP_CLIENT_ID: params.cognitoAppClientId,
    COGNITO_ISSUER_URL: params.cognitoIssuerUrl,
    COGNITO_DOMAIN_URL: params.cognitoDomainUrl,
    COGNITO_REGION: config.awsRegion,
    SHARED_CONVERSATIONS_TABLE_NAME: params.sharedConversationsTableName,
    BFF_SESSIONS_TABLE_NAME: params.bffSessionsTableName,
    BFF_COOKIE_SIGNING_KEY_ARN: params.bffCookieSigningKeyArn,
    BFF_COOKIE_DATA_KEY_SECRET_ARN: params.bffCookieDataKeySecretArn,
    BFF_SESSION_TTL_SECONDS: '28800',
    BFF_SESSION_REFRESH_LEEWAY_SECONDS: '60',
    COGNITO_BFF_APP_CLIENT_ID: params.cognitoBFFAppClientId,
    COGNITO_BFF_APP_CLIENT_SECRET_ARN: params.cognitoBFFAppClientSecretArn,
    BFF_AUTH_CALLBACK_URL: config.domainName
      ? `https://${config.domainName}/api/auth/callback`
      : 'http://localhost:8000/auth/callback',
    BFF_POST_LOGIN_REDIRECT_URL: config.domainName
      ? `https://${config.domainName}/`
      : 'http://localhost:4200/',
    INFERENCE_API_URL: params.inferenceApiRuntimeEndpointUrl,
    VOICE_TICKET_REPLAY_TABLE_NAME: params.voiceTicketReplayTableName,
    VOICE_TICKET_SIGNING_SECRET_ARN: params.voiceTicketSigningSecretArn,
  };
}
