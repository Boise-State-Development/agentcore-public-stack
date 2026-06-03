/**
 * IAM grants for the App API Fargate task role.
 *
 * Extracted from the monolithic app-api-service-construct.ts to improve
 * readability. This module exports a single function that attaches all
 * required IAM policy statements to the task role.
 *
 * The grants are grouped by domain:
 *   - Core tables (users, roles, quotas, costs, sessions, OAuth)
 *   - File uploads (S3 + DDB)
 *   - RAG (assistants table, documents bucket, vector store)
 *   - Cognito (user pool admin ops)
 *   - Secrets Manager (auth secret, OAuth secrets, BFF cookie key)
 *   - Artifacts (S3 + DDB + render token)
 *   - Fine-tuning (DDB + S3 + SageMaker + IAM PassRole)
 *   - AgentCore Memory
 *   - Bedrock (title generation)
 *   - SSM (inference-api image tag for runtime endpoint resolution)
 */

import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { AppConfig } from '../../config';
import { PlatformComputeRefs } from '../platform-compute-refs';

export interface AppApiIamGrantsProps {
  scope: Construct;
  config: AppConfig;
  taskRole: iam.IRole;
  /**
   * Typed bundle of every PlatformStack resource this grants
   * function reads from. Replaces the previous in-function
   * `valueForStringParameter` calls — those would deadlock CFN
   * on first deploy because parameter resolution runs before
   * resource creation. See platform-compute-refs.ts.
   */
  refs: PlatformComputeRefs;
  /**
   * AgentCore Memory ARN. Passed in directly because the Memory
   * resource is on PlatformStack but this grant function is
   * called from compute, and the existing wireCompute() flow
   * already threads memoryArn separately. Could be folded into
   * `refs` later if convenient.
   */
  agentCoreMemoryArn: string;
  /**
   * SageMaker fine-tuning execution role ARN. Created by a sibling
   * construct in wireCompute() — passed in here.
   */
  sagemakerExecutionRoleArn: string;
}

/**
 * Attach all IAM grants to the App API task role.
 *
 * This is a pure side-effect function — it mutates the task role's
 * policy by adding statements. Extracted for readability; the grants
 * themselves are byte-identical to the original monolith.
 */
export function grantAppApiPermissions(props: AppApiIamGrantsProps): void {
  const { scope, config, taskRole } = props;

  // ── Assistants table ──
  // The "regular" assistants table was decommissioned — the python
  // app uses the rag-assistants table for both assistant config and
  // their RAG document/vector metadata, via DYNAMODB_ASSISTANTS_TABLE_NAME
  // → /{prefix}/rag/assistants-table-name. Grants on rag-assistants
  // are wired in the RagAssistantsTableAccess block below.

  // ── User settings ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'UserSettingsTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
        'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
        'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
      ],
      resources: [props.refs.userSettingsTable.tableArn, `${props.refs.userSettingsTable.tableArn}/index/*`],
    }),
  );

  // ── User menu links ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'UserMenuLinksTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
        'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
      ],
      resources: [props.refs.userMenuLinksTable.tableArn, `${props.refs.userMenuLinksTable.tableArn}/index/*`],
    }),
  );

  // ── RAG assistants table ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'RagAssistantsTableAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
        'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
        'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
      ],
      resources: [props.refs.ragAssistantsTable.tableArn, `${props.refs.ragAssistantsTable.tableArn}/index/*`],
    }),
  );

  // ── RAG documents bucket ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'RagDocumentsBucketAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject', 's3:PutObject', 's3:DeleteObject',
        's3:ListBucket', 's3:GetBucketLocation',
      ],
      resources: [props.refs.ragDocumentsBucket.bucketArn, `${props.refs.ragDocumentsBucket.bucketArn}/*`],
    }),
  );

  // ── Core tables (OIDC, Users, Roles, API Keys, OAuth) ──
  const coreTables = [
    { sid: 'OidcStateAccess', arn: props.refs.oidcStateTable.tableArn },
    { sid: 'UsersTableAccess', arn: props.refs.usersTable.tableArn },
    { sid: 'AppRolesTableAccess', arn: props.refs.appRolesTable.tableArn },
    { sid: 'ApiKeysTableAccess', arn: props.refs.apiKeysTable.tableArn },
    { sid: 'OAuthProvidersAccess', arn: props.refs.oauthProvidersTable.tableArn },
    { sid: 'OAuthUserTokensAccess', arn: props.refs.oauthUserTokensTable.tableArn },
    { sid: 'UserQuotasAccess', arn: props.refs.userQuotasTable.tableArn },
    { sid: 'QuotaEventsAccess', arn: props.refs.quotaEventsTable.tableArn },
    { sid: 'SessionsMetadataAccess', arn: props.refs.sessionsMetadataTable.tableArn },
    { sid: 'UserCostSummaryAccess', arn: props.refs.userCostSummaryTable.tableArn },
    { sid: 'SystemCostRollupAccess', arn: props.refs.systemCostRollupTable.tableArn },
    { sid: 'ManagedModelsAccess', arn: props.refs.managedModelsTable.tableArn },
    { sid: 'AuthProvidersAccess', arn: props.refs.authProvidersTable.tableArn },
    { sid: 'BffSessionsAccess', arn: props.refs.bffSessionsTable.tableArn },
    { sid: 'VoiceTicketReplayAccess', arn: props.refs.voiceTicketReplayTable.tableArn },
    { sid: 'UserFilesTableAccess', arn: props.refs.fileUploadTable.tableArn },
  ];

  for (const { sid, arn } of coreTables) {
    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid,
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
          'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
          'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
        ],
        resources: [arn, `${arn}/index/*`],
      }),
    );
  }

  // ── File uploads S3 ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'UserFilesBucketAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject', 's3:PutObject', 's3:DeleteObject',
        's3:ListBucket', 's3:GetBucketLocation',
      ],
      resources: [props.refs.fileUploadBucket.bucketArn, `${props.refs.fileUploadBucket.bucketArn}/*`],
    }),
  );

  // ── Secrets Manager ──
  const secrets = [
    props.refs.oauthClientSecretsSecret.secretArn,
    props.refs.authProviderSecretsSecret.secretArn,
    props.refs.voiceTicketSigningSecret.secretArn,
    props.refs.bffCookieDataKeySecret.secretArn,
    props.refs.bffAppClientSecret.secretArn,
  ];
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'SecretsManagerAccess',
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue'],
      resources: secrets.map((s) => `${s}*`),
    }),
  );

  // ── KMS (OAuth token encryption + BFF cookie signing) ──
  // Two separate statements because the access patterns differ:
  //
  //   - OAuth token encryption key: the app encrypts external-MCP
  //     OAuth tokens before persisting them to DDB and decrypts on
  //     read. Needs the full Encrypt + Decrypt + GenerateDataKey
  //     trio.
  //   - BFF cookie signing key: the app NEVER calls KMS directly
  //     on this key. The plaintext data key lives in Secrets
  //     Manager (BFF_COOKIE_DATA_KEY_SECRET_ARN); the cookie codec
  //     fetches the secret via GetSecretValue, which transparently
  //     decrypts the AWS-managed-encrypted secret using this key.
  //     The IAM grant here only exists so SecretsManager can
  //     transparently decrypt the secret value on GetSecretValue —
  //     hence Decrypt-only.
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'OAuthTokenEncryptionKeyAccess',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey'],
      resources: [props.refs.oauthTokenEncryptionKey.keyArn],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'BffCookieSigningKeyDecrypt',
      effect: iam.Effect.ALLOW,
      actions: ['kms:Decrypt'],
      resources: [props.refs.bffCookieSigningKey.keyArn],
    }),
  );

  // ── Cognito admin ops ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'CognitoAdminAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'cognito-idp:AdminGetUser', 'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:AdminDisableUser', 'cognito-idp:AdminEnableUser',
        'cognito-idp:ListUsers', 'cognito-idp:AdminCreateUser',
        'cognito-idp:AdminSetUserPassword', 'cognito-idp:DescribeUserPool',
        'cognito-idp:UpdateUserPool', 'cognito-idp:ListIdentityProviders',
        'cognito-idp:CreateIdentityProvider', 'cognito-idp:UpdateIdentityProvider',
        'cognito-idp:DeleteIdentityProvider', 'cognito-idp:DescribeIdentityProvider',
        'cognito-idp:DescribeUserPoolClient', 'cognito-idp:UpdateUserPoolClient',
      ],
      resources: [props.refs.userPool.userPoolArn],
    }),
  );

  // ── Artifacts (S3 + DDB + render token) ──
  // Sourced from typed PlatformStack refs — see PlatformComputeRefs.
  const artifactsBucketArn = props.refs.artifactsContentBucket.bucketArn;
  const artifactsTableArn = props.refs.artifactsTable.tableArn;
  const artifactRenderTokenSecretArn = props.refs.artifactRenderTokenSecret.secretArn;

  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'ArtifactsBucketReadWrite',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:PutObject', 's3:PutObjectTagging', 's3:ListBucket'],
      resources: [artifactsBucketArn, `${artifactsBucketArn}/*`],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'ArtifactsTableReadWrite',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
                'dynamodb:DeleteItem', 'dynamodb:Query'],
      resources: [artifactsTableArn, `${artifactsTableArn}/index/*`],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'ArtifactRenderTokenRead',
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue'],
      resources: [`${artifactRenderTokenSecretArn}*`],
    }),
  );

  // ── Fine-tuning ──
  // Sourced from typed PlatformStack refs.
  const ftJobsTableArn = props.refs.fineTuningJobsTable.tableArn;
  const ftAccessTableArn = props.refs.fineTuningAccessTable.tableArn;
  const ftDataBucketArn = props.refs.fineTuningDataBucket.bucketArn;
  // sagemaker-execution-role-arn is written by a sibling construct in
  // PlatformStack, so it still comes in via props (it's already a
  // string ref off the SageMakerExecutionRoleConstruct).
  const ftExecRoleArn = props.sagemakerExecutionRoleArn;

  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'FineTuningTablesAccess',
      effect: iam.Effect.ALLOW,
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
                'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
                'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem'],
      resources: [ftJobsTableArn, `${ftJobsTableArn}/index/*`,
                  ftAccessTableArn, `${ftAccessTableArn}/index/*`],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'FineTuningBucketAccess',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject',
                's3:ListBucket', 's3:GetBucketLocation'],
      resources: [ftDataBucketArn, `${ftDataBucketArn}/*`],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'SageMakerJobManagement',
      effect: iam.Effect.ALLOW,
      actions: ['sagemaker:CreateTrainingJob', 'sagemaker:DescribeTrainingJob',
                'sagemaker:StopTrainingJob', 'sagemaker:ListTrainingJobs',
                'sagemaker:CreateTransformJob', 'sagemaker:DescribeTransformJob',
                'sagemaker:StopTransformJob', 'sagemaker:ListTransformJobs'],
      resources: [`arn:aws:sagemaker:${config.awsRegion}:${config.awsAccount}:training-job/${config.projectPrefix}-*`,
                  `arn:aws:sagemaker:${config.awsRegion}:${config.awsAccount}:transform-job/${config.projectPrefix}-*`],
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'PassSageMakerRole',
      effect: iam.Effect.ALLOW,
      actions: ['iam:PassRole'],
      resources: [ftExecRoleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'sagemaker.amazonaws.com' } },
    }),
  );
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'SageMakerLogsRead',
      effect: iam.Effect.ALLOW,
      actions: ['logs:GetLogEvents', 'logs:FilterLogEvents', 'logs:DescribeLogStreams'],
      resources: [`arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/sagemaker/*`],
    }),
  );

  // ── AgentCore Memory ──
  // Memory ARN is passed in directly from the InferenceApi sibling
  // construct (same stack) rather than read from SSM. Reading SSM
  // here would chicken-and-egg on first deploy because both publisher
  // and consumer live in PlatformStack.
  const memoryArn = props.agentCoreMemoryArn;
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'AgentCoreMemoryAccess',
      effect: iam.Effect.ALLOW,
      // Action names mirror the AgentCore Data Plane API surface:
      //   https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_Operations.html
      // Earlier versions of this grant used speculative names like
      // 'CreateMemoryEvent' / 'ListMemoryEvents' / 'RetrieveMemory' that
      // do not exist as IAM actions, so the entire policy was a silent
      // no-op — the App API hit AccessDeniedException on ListEvents.
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
      resources: [memoryArn],
    }),
  );

  // ── Bedrock (title generation) ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'BedrockInvokeModel',
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [`arn:aws:bedrock:${config.awsRegion}::foundation-model/*`],
    }),
  );

  // ── SSM read for inference-api image tag (runtime endpoint resolution) ──
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'SsmReadInferenceImageTag',
      effect: iam.Effect.ALLOW,
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [
        `arn:aws:ssm:${cdk.Stack.of(scope).region}:${cdk.Stack.of(scope).account}:parameter/${config.projectPrefix}/inference-api/image-tag`,
      ],
    }),
  );

  // ── S3 Vectors (RAG query) ──
  const vectorBucketName = props.refs.ragVectorBucketName;
  const vectorIndexName = props.refs.ragVectorIndexName;
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'S3VectorsQueryAccess',
      effect: iam.Effect.ALLOW,
      actions: ['s3vectors:GetVector', 's3vectors:GetVectors',
                's3vectors:ListVectors', 's3vectors:QueryVectors',
                's3vectors:GetIndex', 's3vectors:ListIndexes'],
      resources: [
        `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}`,
        `arn:aws:s3vectors:${config.awsRegion}:${config.awsAccount}:bucket/${vectorBucketName}/index/${vectorIndexName}`,
      ],
    }),
  );

  // ── AgentCore WorkloadIdentity (OAuth vault token minting) ──
  // Grants the App API the data-plane actions used by /connectors/*
  // routes and shared/oauth/agentcore_identity.py:
  //   - GetWorkloadAccessTokenForUserId / GetWorkloadIdentity:
  //     mint a workload token for a specific user.
  //   - GetResourceOauth2Token / CompleteResourceTokenAuth:
  //     start + complete the 3LO consent flow against an external
  //     OAuth provider, then redeem the auth code for a vaulted
  //     token. Without these, /connectors/{id}/{status,initiate,
  //     disconnect,complete} return 503 at runtime.
  //   - Create/Update/Delete/Get/ListOauth2CredentialProvider:
  //     called by shared/oauth/agentcore_registrar.py when an
  //     admin adds, edits, or removes an OAuth provider via the
  //     admin UI. Stored under the default token vault.
  // Resources: scoped to this account's AgentCore Identity surface.
  // Action names verified against
  //   https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrockagentcore.html
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'AgentCoreWorkloadIdentityAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:GetWorkloadAccessTokenForUserId',
        'bedrock-agentcore:GetWorkloadIdentity',
        'bedrock-agentcore:GetResourceOauth2Token',
        'bedrock-agentcore:CompleteResourceTokenAuth',
        'bedrock-agentcore:CreateOauth2CredentialProvider',
        'bedrock-agentcore:UpdateOauth2CredentialProvider',
        'bedrock-agentcore:DeleteOauth2CredentialProvider',
        'bedrock-agentcore:GetOauth2CredentialProvider',
        'bedrock-agentcore:ListOauth2CredentialProviders',
      ],
      resources: [
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*`,
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:token-vault/*/oauth2credentialprovider/*`,
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*`,
        `arn:aws:bedrock-agentcore:${config.awsRegion}:${config.awsAccount}:workload-identity-directory/*/workload-identity/*`,
      ],
    }),
  );

  // ── AgentCore Identity OAuth vault secrets ──
  // CreateOauth2CredentialProvider auto-creates a Secrets Manager
  // secret under bedrock-agentcore-identity!default/oauth2/<id> to
  // hold each provider's clientSecret. The registrar
  // (shared/oauth/agentcore_registrar.py) needs full lifecycle
  // perms on these secrets to add / rotate / remove providers.
  taskRole.addToPrincipalPolicy(
    new iam.PolicyStatement({
      sid: 'AgentCoreIdentityOAuthSecrets',
      effect: iam.Effect.ALLOW,
      actions: [
        'secretsmanager:GetSecretValue',
        'secretsmanager:DescribeSecret',
        'secretsmanager:CreateSecret',
        'secretsmanager:PutSecretValue',
        'secretsmanager:UpdateSecret',
        'secretsmanager:DeleteSecret',
        'secretsmanager:TagResource',
      ],
      resources: [
        `arn:aws:secretsmanager:${config.awsRegion}:${config.awsAccount}:secret:bedrock-agentcore-identity!default/oauth2/*`,
      ],
    }),
  );
}
