import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as xray from 'aws-cdk-lib/aws-xray';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as path from 'path';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, getTruncatedResourceName, applyStandardTags, buildCorsOrigins } from '../../config';
import { AlarmFactory } from '../observability/alarm-factory';
import { PlatformComputeRefs } from '../platform-compute-refs';
import {
  createRuntimeExecutionRole,
} from './inference-api-iam-roles';

export interface InferenceAgentCoreConstructProps {
  config: AppConfig;
  /**
   * Typed bundle of every PlatformStack resource ref this construct
   * needs at synth time. Replaces the in-construct
   * `valueForStringParameter` calls — same-stack SSM reads cause a
   * CFN parameter-resolution deadlock on first deploy.
   */
  refs: PlatformComputeRefs;
  /**
   * AgentCore Memory ARN. Sourced from PlatformStack as a typed
   * typed construct ref. Memory itself was hoisted to PlatformStack —
   * see `AgentCoreMemoryConstruct` — because it has no code, takes
   * 5-15 minutes to create, and shouldn't be touched on every
   * Backend deploy.
   */
  memoryArn: string;
  /** AgentCore Memory ID — same provenance as memoryArn. */
  memoryId: string;
  /**
   * AgentCore Code Interpreter ARN. Sourced from PlatformStack as
   * a typed typed construct ref (CodeInterpreter hoisted to Platform).
   */
  codeInterpreterArn: string;
  /** AgentCore Code Interpreter ID — same provenance as codeInterpreterArn. */
  codeInterpreterId: string;
  /**
   * AgentCore Browser ARN. Sourced from PlatformStack as a typed
   * typed construct ref (Browser hoisted to Platform).
   */
  browserArn: string;
  /** AgentCore Browser ID — same provenance as browserArn. */
  browserId: string;
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * InferenceAgentCoreConstruct — AgentCore Runtime.
 *
 * owns just the Runtime + its execution role + Runtime observability.
 * Memory, Code Interpreter, and Browser were hoisted to PlatformStack
 * (each with its own construct under `agentcore/`); this construct
 * receives them as typed props.
 *
 * IAM roles are created via inference-api-iam-roles.ts (extracted).
 */
export class InferenceAgentCoreConstruct extends Construct {
  public readonly runtime: bedrock.CfnRuntime;
  /**
   * Full Bedrock AgentCore Runtime endpoint URL. Exposed so other
   * compute constructs (notably the App API) can wire it via
   * direct construct refs instead of round-tripping through SSM,
   * which would chicken-and-egg on a same-stack first deploy.
   */
  public readonly runtimeEndpointUrl: string;
  /**
   * The log group the runtime actually writes to.
   *
   * Bedrock AgentCore creates this itself, named after the runtime *id*
   * (which carries an AWS-assigned suffix) plus the endpoint qualifier —
   * NOT after our project prefix. Exposed so dashboards elsewhere in the
   * stack query the group that has data in it.
   */
  public readonly runtimeLogGroupName: string;
  /**
   * The `Name` dimension value on every AgentCore Runtime metric:
   * `{agentRuntimeName}::{endpointName}`.
   *
   * Exposed so the platform dashboard binds to the SAME string this construct's
   * own alarms use. Two places deriving it independently is how one of them ends
   * up watching a stream that is never published.
   */
  public readonly runtimeMetricName: string;

  constructor(scope: Construct, id: string, props: InferenceAgentCoreConstructProps) {
    super(scope, id);

    const { config } = props;

    applyStandardTags(cdk.Stack.of(this), config);

    // ── Bootstrap container image + SSM-resolved live image ──
    // The Runtime's containerUri is read from an SSM parameter at
    // CFN deploy time, NOT baked into the synthesized template.
    // When CFN updates the Runtime (any property change — env var,
    // authorizer config, network config, etc.), it resolves the
    // SSM parameter and uses whatever URI is currently there, which
    // is the latest image the build pipeline pushed. The bootstrap
    // stub is never reverted onto a live Runtime.
    //
    // Bootstrap responsibility:
    //   - First-deploy seed lives in scripts/stack-bootstrap/
    //     seed-image-tags.sh, which runs before `cdk deploy` in
    //     scripts/platform/deploy.sh. It pushes the bootstrap image
    //     below to the cdk-assets ECR repo (via cdk-assets publish)
    //     and writes its URI to SSM if the parameter doesn't exist.
    //   - Subsequent runs: the build pipeline (backend.yml's
    //     deploy-inference-api-code → deploy-runtime-image-one.sh)
    //     overwrites the SSM tag with the per-service ECR URI on
    //     every push.
    //
    // The DockerImageAsset is kept (not directly referenced by the
    // Runtime resource anymore) so cdk-assets continues to publish
    // it for the seed step. The CfnOutput exposes its assetHash so
    // the seed script can construct the cdk-assets URI without
    // needing to parse Fn::Sub from the template.
    const bootstrapImage = new ecr_assets.DockerImageAsset(this, 'AgentCoreRuntimeBootstrap', {
      directory: path.resolve(
        __dirname, '..', '..', '..', 'bootstrap-assets', 'inference-api',
      ),
      platform: ecr_assets.Platform.LINUX_ARM64,
    });
    new cdk.CfnOutput(this, 'InferenceApiBootstrapImageHash', {
      description: 'cdk-assets image tag for the inference-api bootstrap container. Consumed by scripts/stack-bootstrap/seed-image-tags.sh on first deploy.',
      value: bootstrapImage.assetHash,
    });

    const inferenceApiImageTagSsmPath = `/${config.projectPrefix}/inference-api/image-tag`;
    const inferenceApiImageUri = ssm.StringParameter.valueForStringParameter(
      this, inferenceApiImageTagSsmPath,
    );

    // The project's ECR repo (where the workflow ships real images
    // to). Imported for IAM grants only — CDK doesn't reference any
    // image tag in this repo at synth time anymore.
    const ecrRepository = ecr.Repository.fromRepositoryName(
      this, 'InferenceApiRepository', getResourceName(config, 'inference-api'));

    // ── IAM roles (extracted into inference-api-iam-roles.ts) ──
    const runtimeExecutionRole = createRuntimeExecutionRole(this, config, props.refs);
    // Memory / Code Interpreter / Browser execution roles were hoisted
    // to PlatformStack alongside their resources (Phase 1 of the
    //   - constructs/agentcore/memory-construct.ts
    //   - constructs/agentcore/code-interpreter-construct.ts
    //   - constructs/agentcore/browser-construct.ts

    // Grant the Runtime execution role pull rights on the project's
    // inference-api ECR repo so `update-agent-runtime` can switch the
    // Runtime over to a real image. The bootstrap image's pull
    // rights on cdk-assets are auto-granted by DockerImageAsset.
    bootstrapImage.repository.grantPull(runtimeExecutionRole);
    ecrRepository.grantPull(runtimeExecutionRole);

    // ── Additional SSM reads needed by the runtime container env ──
    const authProviderSecretsArn = props.refs.authProviderSecretsSecret.secretArn;
    const oauthTokenEncryptionKeyArn = props.refs.oauthTokenEncryptionKey.keyArn;
    const oauthClientSecretsArn = props.refs.oauthClientSecretsSecret.secretArn;

    // Memory + Code Interpreter + Browser are owned by PlatformStack
    // IDs flow in via typed props (`props.memoryArn`, etc.). We grant
    // the Runtime role permission against those ARNs below.

    // ============================================================
    // AgentCore Runtime
    // ============================================================

    // Grant Runtime permission to access Memory.
    // Action list mirrors the AgentCore Data Plane API surface — see
    // https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_Operations.html
    // GetMemory and GetMemoryStrategies are control-plane shapes that do
    // not exist as separate IAM actions; the same data-plane policy
    // covers them. RetrieveMemory / ListMemorySessions / GetMemorySession
    // were also speculative and removed.
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'MemoryAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:GetEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:ListActors',
        'bedrock-agentcore:ListSessions',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:ListMemoryRecords',
      ],
      resources: [props.memoryArn],
    }));

    // Grant Runtime permission to use the Custom Code Interpreter.
    // Action list matches AWS's documented policy for Code Interpreter access
    // (see docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
    // code-interpreter-getting-started.html). Scoped to this stack's Custom
    // Code Interpreter only — we don't need account-wide discovery perms.
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CodeInterpreterAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:StartCodeInterpreterSession',
        'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession',
        'bedrock-agentcore:GetCodeInterpreter',
        'bedrock-agentcore:GetCodeInterpreterSession',
        'bedrock-agentcore:ListCodeInterpreterSessions',
      ],
      resources: [props.codeInterpreterArn],
    }));

    // Grant Runtime permission to use Browser.
    // Real browser actions per the Service Authorization Reference:
    //   StartBrowserSession, GetBrowserSession, ListBrowserSessions,
    //   StopBrowserSession, ConnectBrowserAutomationStream,
    //   ConnectBrowserLiveViewStream, UpdateBrowserStream,
    //   SaveBrowserSessionProfile.
    // 'InvokeBrowser' is NOT a real action and was a silent no-op.
    runtimeExecutionRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BrowserAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:StartBrowserSession',
        'bedrock-agentcore:GetBrowserSession',
        'bedrock-agentcore:ListBrowserSessions',
        'bedrock-agentcore:StopBrowserSession',
        'bedrock-agentcore:ConnectBrowserAutomationStream',
        'bedrock-agentcore:ConnectBrowserLiveViewStream',
        'bedrock-agentcore:UpdateBrowserStream',
      ],
      resources: [props.browserArn],
    }));

    // ============================================================
    // Import Cognito SSM Parameters for JWT Authorizer
    // ============================================================

    const cognitoUserPoolId = props.refs.userPool.userPoolId;
    // Phase 7 retired the public PKCE SPA client; the BFF confidential
    // client is the only one left. The runtime authorizer's allowed-clients
    // list now points at it so tokens minted via the BFF flow are accepted
    // when the chat proxy on app-api forwards them to /invocations.
    const cognitoAppClientId = props.refs.bffAppClient.userPoolClientId;

    // Construct Cognito OIDC discovery URL
    const cognitoDiscoveryUrl = `https://cognito-idp.${config.awsRegion}.amazonaws.com/${cognitoUserPoolId}/.well-known/openid-configuration`;

    // ============================================================
    // Import SSM Parameters for Runtime Environment Variables
    // ============================================================

    // DynamoDB table names (the ARNs are already imported above for IAM)
    const usersTableName = props.refs.usersTable.tableName;
    const appRolesTableName = props.refs.appRolesTable.tableName;
    const oidcStateTableName = props.refs.oidcStateTable.tableName;
    const apiKeysTableName = props.refs.apiKeysTable.tableName;
    const oauthProvidersTableName = props.refs.oauthProvidersTable.tableName;
    const oauthUserTokensTableName = props.refs.oauthUserTokensTable.tableName;
    const assistantsTableName = props.refs.ragAssistantsTable.tableName;
    const userQuotasTableName = props.refs.userQuotasTable.tableName;
    const quotaEventsTableName = props.refs.quotaEventsTable.tableName;
    const sessionsMetadataTableName = props.refs.sessionsMetadataTable.tableName;
    const userCostSummaryTableName = props.refs.userCostSummaryTable.tableName;
    const systemCostRollupTableName = props.refs.systemCostRollupTable.tableName;
    const managedModelsTableName = props.refs.managedModelsTable.tableName;
    const userSettingsTableName = props.refs.userSettingsTable.tableName;
    const authProvidersTableName = props.refs.authProvidersTable.tableName;
    const userFilesTableName = props.refs.fileUploadTable.tableName;
    const systemPromptsTableName = props.refs.systemPromptsTable.tableName;

    // S3 / RAG
    const vectorBucketName = props.refs.ragVectorBucketName;
    const vectorIndexName = props.refs.ragVectorIndexName;

    // Frontend CORS origins — single source: buildCorsOrigins (from CDK_DOMAIN_NAME)
    const corsOrigins = buildCorsOrigins(config, config.inferenceApi.additionalCorsOrigins).join(',');

    // ============================================================
    // Single CDK-Managed AgentCore Runtime with Cognito JWT Authorizer
    // ============================================================

    // Hoisted to a const because the CloudWatch `Name` dimension for every
    // runtime metric is `{agentRuntimeName}::{endpointName}`. Deriving both the
    // resource name and the alarm dimension from one expression is what keeps
    // the alarms bound if the naming ever changes — an alarm whose dimension no
    // longer matches a published stream does not fail, it just goes quiet.
    const agentRuntimeName = getResourceName(config, 'agentcore_runtime').replace(/-/g, '_');

    this.runtime = new bedrock.CfnRuntime(this, 'AgentCoreRuntime', {
      agentRuntimeName,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: inferenceApiImageUri,
        },
      },
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: cognitoDiscoveryUrl,
          allowedClients: [cognitoAppClientId],
        },
      },
      roleArn: runtimeExecutionRole.roleArn,
      networkConfiguration: {
        networkMode: 'PUBLIC',
      },
      // HTTP protocol supports both REST (/invocations) and WebSocket (/ws) endpoints
      protocolConfiguration: 'HTTP',
      requestHeaderConfiguration: {
        requestHeaderAllowlist: ['Authorization'],
      },
      environmentVariables: {
        // Basic configuration
        LOG_LEVEL: 'INFO',
        PROJECT_PREFIX: config.projectPrefix,
        AWS_DEFAULT_REGION: config.awsRegion,

        // DynamoDB tables
        DYNAMODB_USERS_TABLE_NAME: usersTableName,
        DYNAMODB_APP_ROLES_TABLE_NAME: appRolesTableName,
        DYNAMODB_OIDC_STATE_TABLE_NAME: oidcStateTableName,
        DYNAMODB_API_KEYS_TABLE_NAME: apiKeysTableName,
        DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME: oauthProvidersTableName,
        DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME: oauthUserTokensTableName,
        DYNAMODB_ASSISTANTS_TABLE_NAME: assistantsTableName,

        // Quota & cost tracking tables
        DYNAMODB_QUOTA_TABLE: userQuotasTableName,
        DYNAMODB_QUOTA_EVENTS_TABLE: quotaEventsTableName,
        DYNAMODB_SESSIONS_METADATA_TABLE_NAME: sessionsMetadataTableName,
        DYNAMODB_COST_SUMMARY_TABLE_NAME: userCostSummaryTableName,
        DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: systemCostRollupTableName,
        DYNAMODB_MANAGED_MODELS_TABLE_NAME: managedModelsTableName,
        DYNAMODB_USER_SETTINGS_TABLE_NAME: userSettingsTableName,
        DYNAMODB_USER_FILES_TABLE_NAME: userFilesTableName,
        // Bucket the runtime writes generated Word docs to (create/modify
        // word-document tools). Without this the tool falls back to the
        // literal "user-files" default and PutObject is AccessDenied. The
        // runtime role's UserFilesBucketAccess statement already grants
        // Get/Put/Delete/List on this bucket.
        S3_USER_FILES_BUCKET_NAME: props.refs.fileUploadBucket.bucketName,
        DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME: systemPromptsTableName,

        // Auth providers
        DYNAMODB_AUTH_PROVIDERS_TABLE_NAME: authProvidersTableName,
        AUTH_PROVIDER_SECRETS_ARN: authProviderSecretsArn,

        // OAuth configuration
        OAUTH_TOKEN_ENCRYPTION_KEY_ARN: oauthTokenEncryptionKeyArn,
        OAUTH_CLIENT_SECRETS_ARN: oauthClientSecretsArn,

        // AgentCore resources
        AGENTCORE_MEMORY_ID: props.memoryId,
        MEMORY_ARN: props.memoryArn,
        AGENTCORE_CODE_INTERPRETER_ID: props.codeInterpreterId,
        BROWSER_ID: props.browserId,

        // Gateway inbound auth mode. Sourced from the SAME config value that
        // builds the Gateway's authorizer, so the agent's data-plane auth and
        // the deployed authorizerType cannot drift: 'jwt' → the agent sends the
        // user's Cognito access token as a Bearer token; 'iam' → SigV4.
        AGENTCORE_GATEWAY_INBOUND_AUTH: config.gateway.inboundAuth,

        // RFC 8693 token exchange. Spread in only when configured, so a
        // deployment that does not use it gets no extra environment variables at
        // all — no diff to its runtime definition. The exchange runs here rather
        // than in the Gateway because AgentCore's outbound OAuth credential
        // provider has no token-exchange grant.
        ...(config.tokenExchange
          ? {
              TOKEN_EXCHANGE_URL: config.tokenExchange.url,
              TOKEN_EXCHANGE_CLIENT_ID: config.tokenExchange.clientId,
              TOKEN_EXCHANGE_SECRET_ID:
                props.refs.tokenExchangeSecret?.secretName ?? '',
            }
          : {}),

        // S3 storage
        S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME: vectorBucketName,
        S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME: vectorIndexName,
        // Assistants KB documents bucket — needed by the agent's spreadsheet
        // analysis tool to download files from S3 before pushing them into
        // the Code Interpreter sandbox. Imported from RagIngestionStack via
        // SSM (same parameter app-api uses). Without this the agent fails
        // with "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME not configured".
        S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME: props.refs.ragDocumentsBucket.bucketName,

        // Skill reference-file bucket (admin-managed Skills). Provisioned now
        // (read grant below) so the PR-6 runtime can read a skill's reference
        // files at dispatch time; no code consumes it yet.
        S3_SKILL_RESOURCES_BUCKET_NAME: props.refs.skillResourcesBucket.bucketName,

        // Memory Spaces storage. The runtime writes memory in a later PR
        // (readwrite grant below); read by apis/shared/memory/*.
        S3_MEMORY_SPACES_BUCKET_NAME: props.refs.memorySpacesBucket.bucketName,
        DYNAMODB_MEMORY_SPACES_TABLE_NAME: props.refs.memorySpacesTable.tableName,
        MEMORY_SPACES_ENABLED: config.memorySpaces.enabled ? 'true' : 'false',

        // Skills v2 (default ON with a kill switch, mirroring the app-api flag).
        // Gates skill resolution on the invocation path — the AgentSkills plugin's
        // <available_skills> block, the `skills` activation tool, and
        // `read_skill_file`. Must stay in step with app-api: design-time refuses to
        // bind a skill while the flag is off there, so a mismatch would let an Agent
        // be built with skills the runtime then blocks.
        SKILLS_ENABLED: config.skills.enabled ? 'true' : 'false',

        // Agent Designer harness resolution (Phase 3): the runtime resolves an
        // Agent's bindings + modelConfig at invocation. Gates that resolution;
        // default off, mirrors the app-api flag. Without it the harness ignores
        // bindings entirely (today's behavior).
        AGENTS_API_ENABLED: config.agents.enabled ? 'true' : 'false',

        // Authentication
        ENABLE_QUOTA_ENFORCEMENT: 'true',

        // ⚠️ NO ROOM FOR NEW VARIABLES HERE — see the assertion in
        // test/inference-agentcore-construct.test.ts. `AWS::BedrockAgentCore::Runtime`
        // caps EnvironmentVariables at 50 and this construct is AT the cap.
        // Adding one more fails CloudFormation's *changeset validation* — after
        // synth, after tsc, after jest, after CI is green. It broke the dev
        // Platform Stack deploy on 2026-08-05 (`maximum size: [50], found: [51]`,
        // adding QUOTA_RUNWAY_ENABLED for #833 PR-5).
        //
        // To add a flag you must first free a slot: retire a dead variable, or
        // fold several booleans into one delimited FEATURE_FLAGS value. A
        // code-level flag that reads `os.environ` and defaults ON needs no entry
        // here at all — that is why QUOTA_RUNWAY_ENABLED is absent and the quota
        // runway is still on. Setting such a flag to a non-default value in a
        // deployed environment requires an out-of-band Runtime update until a
        // slot is freed.

        // Directories
        UPLOAD_DIR: '/tmp/uploads',
        OUTPUT_DIR: '/tmp/output',
        GENERATED_IMAGES_DIR: '/tmp/generated_images',

        // URLs
        FRONTEND_URL: config.domainName ? `https://${config.domainName}` : 'http://localhost:4200',
        CORS_ORIGINS: corsOrigins,

        // OAuth2 callback URL fallback for the agent loop's consent flow.
        // Frontends send `OAuth2CallbackUrl` on /invocations, but the
        // AgentCore Runtime gateway strips custom headers before they reach
        // the container, so `BedrockAgentCoreContext.get_oauth2_callback_url()`
        // is empty here. `_resolve_callback_url` falls back to this env var —
        // see apis/shared/oauth/agentcore_identity.py.
        AGENTCORE_LOCAL_OAUTH_CALLBACK_URL: config.domainName
          ? `https://${config.domainName}/oauth-complete`
          : 'http://localhost:4200/oauth-complete',

        // Shared platform workload identity (created in InfrastructureStack).
        // Both inference-api and app-api mint user-scoped workload tokens
        // against this identity so they share a single OAuth token vault.
        // The runtime auto-creates its own service-linked identity, but it
        // cannot be shared cross-service — see PlatformStack and
        // `_resolve_workload_token` in apis/shared/oauth/agentcore_identity.py.
        AGENTCORE_RUNTIME_WORKLOAD_NAME: props.refs.platformWorkloadIdentity.name,

        // MCP Apps sandbox-proxy origin (PR #7 of
        // docs/kaizen/scoping/mcp-apps-host-renderer.md). The agent emits
        // it on the `ui_resource` SSE event as `sandboxOrigin` — the
        // cross-origin shell the SPA frames a hosted App in. The
        // mcp-sandbox stack is always provisioned, so the value is always
        // available via the platform refs.
        AGENTCORE_MCP_APPS_SANDBOX_ORIGIN: props.refs.mcpSandboxProxyOrigin,
      },
    });
    this.runtime.node.addDependency(runtimeExecutionRole);

    // ============================================================
    // Observability: CloudWatch Log Group for Runtime
    // ============================================================

    // The runtime's log group is created by the AgentCore service, not by us,
    // and is named after the runtime *id* + endpoint qualifier — e.g.
    // `/aws/bedrock-agentcore/runtimes/<prefix>_agentcore_runtime-Z6D3HsHKs6-DEFAULT`.
    //
    // We used to declare a LogGroup at `/aws/bedrock-agentcore/runtimes/<prefix>`
    // and point every Logs Insights widget at it. Nothing ever wrote there:
    // measured in dev, that group held **0 bytes** while the service's own group
    // held 229 MB, so all three widgets returned empty results and read as
    // "no errors" / "no traffic" rather than as a broken query. Removing it also
    // drops a retention policy that never applied to anything.
    //
    // ⚠️ Retention on the real group cannot be set with a CDK `LogGroup`
    // construct, because the group is created by the AgentCore service rather
    // than by CloudFormation — declaring one here would either collide on
    // create or manage a second, empty group. Left unmanaged, it grows forever:
    // dev alone carries several such groups in the hundreds of MB. Tracked as a
    // W5 follow-up in docs/one-pagers/cost-effectiveness-roadmap.md, closed by
    // the custom resource below.
    this.runtimeLogGroupName =
      `/aws/bedrock-agentcore/runtimes/${this.runtime.attrAgentRuntimeId}-DEFAULT`;
    this.runtimeMetricName = `${agentRuntimeName}::DEFAULT`;

    // Apply the platform's configured retention to that service-created group.
    //
    // `logs:PutRetentionPolicy` is idempotent and, usefully, CREATES the log
    // group if it does not exist yet — which matters on a first deploy, when the
    // runtime has been created but has not yet been invoked and so has never
    // written a log line. Without that behaviour this would race the first
    // invocation.
    //
    // onUpdate as well as onCreate so that changing
    // `observability.logRetentionDays` actually re-applies. No onDelete: the
    // group belongs to the AgentCore service, and removing a retention policy on
    // the way out would silently convert it back to "keep forever", which is the
    // cost problem this exists to fix.
    const runtimeLogRetention = new cr.AwsCustomResource(this, 'RuntimeLogRetention', {
      onCreate: {
        service: 'CloudWatchLogs',
        action: 'putRetentionPolicy',
        parameters: {
          logGroupName: this.runtimeLogGroupName,
          retentionInDays: config.observability.logRetentionDays,
        },
        // Changing the retention value changes this id, which is what makes CFN
        // re-invoke the call rather than treating the resource as unchanged.
        physicalResourceId: cr.PhysicalResourceId.of(
          `${this.runtimeLogGroupName}-retention-${config.observability.logRetentionDays}`,
        ),
      },
      onUpdate: {
        service: 'CloudWatchLogs',
        action: 'putRetentionPolicy',
        parameters: {
          logGroupName: this.runtimeLogGroupName,
          retentionInDays: config.observability.logRetentionDays,
        },
        physicalResourceId: cr.PhysicalResourceId.of(
          `${this.runtimeLogGroupName}-retention-${config.observability.logRetentionDays}`,
        ),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['logs:PutRetentionPolicy', 'logs:CreateLogGroup'],
          // Scoped to this runtime's own group. The trailing :* matches the
          // log-stream ARN form CloudWatch Logs requires for group-level calls.
          resources: [
            `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:${this.runtimeLogGroupName}:*`,
          ],
        }),
      ]),
      installLatestAwsSdk: false,
    });
    runtimeLogRetention.node.addDependency(this.runtime);

    // NOTE: X-Ray TransactionSearchConfig is an account-level singleton.
    // It cannot be created via CloudFormation if it already exists.
    // See 2d in .github/docs/deploy/step-02-aws-setup.md for more information

    // ============================================================
    // Observability: Vended Log Deliveries for AgentCore Resources
    // ============================================================
    // Memory observability moved to PlatformStack.
    //
    // The vended log delivery for Memory APPLICATION_LOGS + TRACES
    // now lives in `AgentCoreMemoryConstruct` alongside the Memory
    // resource itself, since they're inseparable from the Memory's
    // lifecycle.
    // ============================================================

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
        // Single configured values, not a production ternary. The old
        // non-production branch was fixedRate 1.0 / reservoir 50 — a recorded
        // trace for EVERY agent invocation, at $5 per million traces, inherited
        // by any fork that never set `production`. Defaults are now 0.01 / 1.
        fixedRate: config.observability.xraySamplingRate,
        reservoirSize: config.observability.xraySamplingReservoir,
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
        notificationsEnabled: config.observability.xrayInsightsNotifications,
      },
    });

    // ============================================================
    // Observability: CloudWatch Dashboard
    // ============================================================

    const dashboard = new cloudwatch.Dashboard(this, 'AgentCoreObservabilityDashboard', {
      dashboardName: getResourceName(config, 'agentcore-observability'),
      defaultInterval: cdk.Duration.hours(3),
    });

    // ── Metric binding (verified against the live account, not inferred) ──
    //
    // This block previously used namespace `bedrock-agentcore` with metric names
    // `InvocationCount`, `InvocationErrors`, and `InvocationLatency`. A
    // read-only `aws cloudwatch list-metrics` sweep proved all three are wrong:
    //
    //   * `bedrock-agentcore` DOES exist, but holds only the OpenTelemetry /
    //     Strands APPLICATION metrics the agent emits itself — gen_ai.*,
    //     http.server.*, strands.event_loop.*, strands.tool.*.
    //   * `InvocationCount` / `InvocationErrors` / `InvocationLatency` exist in
    //     NO namespace in the account.
    //   * `AWS/BedrockAgentCore` (unhyphenated) has zero metric streams.
    //
    // So both alarms below sat in INSUFFICIENT_DATA from the day they were
    // created, and every dashboard widget rendered empty — which reads as
    // "no errors, no traffic" rather than "this query is broken". That is the
    // same failure this repo already hit with a guessed log-group name, and it
    // is the reason the metric names here are pinned by a test.
    //
    // The service metrics live in `AWS/Bedrock-AgentCore` and are DIMENSIONED.
    // An undimensioned metric in this namespace matches nothing, because every
    // published stream carries at least an Operation.
    const agentCoreNamespace = 'AWS/Bedrock-AgentCore';

    // The runtime's own three-dimension set. The `Name` dimension is
    // `{agentRuntimeName}::{endpointName}` and the endpoint is DEFAULT, matching
    // the qualifier already used for the log group above.
    //
    // A four-dimension variant also exists that adds ComputeType=MicroVM.
    // Deliberately not used: the compute type is an AgentCore implementation
    // detail, and pinning an alarm to it would silently unbind the alarm if AWS
    // ever changed how the runtime is executed.
    const runtimeDimensions = {
      Resource: this.runtime.attrAgentRuntimeArn,
      Operation: 'InvokeAgentRuntime',
      Name: this.runtimeMetricName,
    };

    // No `label` here on purpose. Setting one forces CDK to render an alarm's
    // metric as a Metrics[] array rather than flat Namespace/MetricName/
    // ExtendedStatistic properties, which breaks straightforward assertions on
    // the binding — and the binding is the thing that was wrong before.
    // CloudWatch labels percentile series adequately on its own.
    const runtimeMetric = (
      metricName: string,
      statistic: string,
    ) => new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName,
      dimensionsMap: runtimeDimensions,
      statistic,
      period: cdk.Duration.minutes(5),
    });

    const invocationsMetric = runtimeMetric('Invocations', 'Sum');
    const systemErrorsMetric = runtimeMetric('SystemErrors', 'Sum');
    const userErrorsMetric = runtimeMetric('UserErrors', 'Sum');
    const throttlesMetric = runtimeMetric('Throttles', 'Sum');
    const sessionsMetric = runtimeMetric('Sessions', 'Sum');
    const latencyP50Metric = runtimeMetric('Latency', 'p50');
    const latencyP90Metric = runtimeMetric('Latency', 'p90');
    const latencyP99Metric = runtimeMetric('Latency', 'p99');

    // Real-time gauge of concurrent sessions, published once a minute per
    // service type and dimensioned only by Service. This is the saturation
    // signal for session quota consumption — `Sessions` is a cumulative
    // creation counter and cannot answer "how many are running right now".
    const activeSessionsMetric = new cloudwatch.Metric({
      namespace: agentCoreNamespace,
      metricName: 'ActiveSessionCount',
      dimensionsMap: { Service: 'AgentCore.Runtime' },
      statistic: 'Maximum',
      period: cdk.Duration.minutes(5),
    });

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# AgentCore Runtime Observability\n**Project:** ${config.projectPrefix} | **Region:** ${config.awsRegion} | **Namespace:** \`${agentCoreNamespace}\`\n\nLLM token usage and prompt-cache efficiency live on the **${getResourceName(config, 'prompt-cache-observability')}** dashboard — the token metrics in this namespace are Memory-strategy counters, not model tokens.`,
        width: 24,
        height: 2,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Invocations & Errors',
        left: [invocationsMetric],
        right: [systemErrorsMetric, userErrorsMetric, throttlesMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Invocation Latency (p50 / p90 / p99) — SSE, so seconds are normal',
        left: [latencyP50Metric, latencyP90Metric, latencyP99Metric],
        width: 12,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Sessions created vs currently active',
        left: [sessionsMetric],
        right: [activeSessionsMetric],
        width: 12,
        height: 6,
      }),
      new cloudwatch.LogQueryWidget({
        title: 'Recent Runtime Errors',
        logGroupNames: [this.runtimeLogGroupName],
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

    const alarms = new AlarmFactory(this, config, props.alarmTopic);

    // Split by blame. SystemErrors are AgentCore's fault and mean escalate to
    // AWS; UserErrors are ours and mean a malformed request, a missing
    // permission, or a payload the runtime rejected. Folding them together
    // would produce one alarm whose first diagnostic step is always "find out
    // which kind" — which is what the split answers for free.
    alarms.alarm('AgentCoreSystemErrorAlarm', {
      name: 'agentcore-system-errors',
      alarmDescription:
        'AgentCore Runtime returned server-side errors — AWS-side fault, not application code',
      metric: systemErrorsMetric,
      threshold: config.observability.agentCoreErrorThreshold,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Retains the original logical id and alarm name so the existing alarm is
    // UPDATED in place rather than replaced — it just finally points at a
    // metric that exists.
    alarms.alarm('AgentCoreHighErrorRateAlarm', {
      name: 'agentcore-high-error-rate',
      alarmDescription:
        'AgentCore Runtime returned client-side (user) errors above threshold — malformed requests, missing permissions, or rejected payloads',
      metric: userErrorsMetric,
      threshold: config.observability.agentCoreErrorThreshold,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Throttling means the account is over its AgentCore TPS or session quota.
    // Threshold 0 and a short window: unlike an error, a throttle is never
    // ambiguous and never self-corrects without either less traffic or a quota
    // increase, and quota increases take lead time.
    alarms.alarm('AgentCoreThrottleAlarm', {
      name: 'agentcore-throttles',
      alarmDescription:
        'AgentCore Runtime is throttling invocations — the account is at its TPS or session quota, which needs a quota increase rather than a retry',
      metric: throttlesMetric,
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarms.alarm('AgentCoreHighLatencyAlarm', {
      name: 'agentcore-high-latency',
      alarmDescription: 'AgentCore Runtime p99 latency exceeded threshold',
      metric: latencyP99Metric,
      // Streaming-aware floor (default 120000), NOT the old hardcoded 30000.
      //
      // Units are Milliseconds — verified against live data, because this is
      // exactly where a 1000x threshold error hides (the ALB's
      // TargetResponseTime, by contrast, is reported in SECONDS).
      //
      // Measured over 14 days in dev: average turn 3.0-4.5s, with daily maxima
      // reaching 16.7s, 16.9s, and 24.4s. The old 30s threshold sat just above
      // the observed maximum, so a single slow-but-healthy agent turn could trip
      // it — and an alarm that fires on normal behaviour earns a mute rule and
      // then means nothing. 120s is well clear of a legitimate long turn while
      // still catching a genuinely hung request.
      threshold: config.observability.agentCoreLatencyMs,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // SSM Parameters for Cross-Stack References
    // ============================================================
    
    // Export runtime execution role ARN for Lambda-created runtimes


    new ssm.StringParameter(this, 'RuntimeIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/runtime-id`,
      stringValue: this.runtime.attrAgentRuntimeId,
      description: 'AgentCore Runtime ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    // The runtime auto-creates its own service-linked workload identity, but
    // we don't surface it: it's only mintable from inside the runtime
    // container, so cross-service callers can't use it. Both APIs share the
    // platform workload identity defined in InfrastructureStack instead.

    // Construct the full runtime endpoint URL for frontend consumption
    const runtimeEndpointUrl = cdk.Fn.sub(
      'https://bedrock-agentcore.${AWS::Region}.amazonaws.com/runtimes/${RuntimeArn}',
      { RuntimeArn: this.runtime.attrAgentRuntimeArn }
    );
    this.runtimeEndpointUrl = runtimeEndpointUrl;

    
    // Memory / Code Interpreter / Browser SSM publications were
    // hoisted to PlatformStack alongside the resources themselves
    // (see constructs/agentcore/*.ts). The Runtime continues to
    // consume them via typed cross-stack props.

    // Export ECR repository URI for Lambda-created runtimes

    // Export observability log group name

    // ============================================================
    // CloudFormation Outputs
    // ============================================================

    // Memory / Code Interpreter / Browser outputs were hoisted to
    // PlatformStack alongside their resources; no need to re-emit
    // here. Runtime-specific outputs follow.

    new cdk.CfnOutput(this, 'AgentCoreRuntimeArn', {
      value: this.runtime.attrAgentRuntimeArn,
      description: 'AgentCore Runtime ARN',
      exportName: `${config.projectPrefix}-AgentCoreRuntimeArn`,
    });

    new cdk.CfnOutput(this, 'AgentCoreRuntimeId', {
      value: this.runtime.attrAgentRuntimeId,
      description: 'AgentCore Runtime ID',
      exportName: `${config.projectPrefix}-AgentCoreRuntimeId`,
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
      value: this.runtimeLogGroupName,
      description: 'CloudWatch Log Group for AgentCore Runtime',
      exportName: `${config.projectPrefix}-AgentCoreRuntimeLogGroup`,
    });
   }
}
