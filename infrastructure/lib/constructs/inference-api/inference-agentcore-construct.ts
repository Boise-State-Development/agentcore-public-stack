import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as xray from 'aws-cdk-lib/aws-xray';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as path from 'path';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, getTruncatedResourceName, applyStandardTags, buildCorsOrigins } from '../../config';
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

    this.runtime = new bedrock.CfnRuntime(this, 'AgentCoreRuntime', {
      agentRuntimeName: getResourceName(config, 'agentcore_runtime').replace(/-/g, '_'),
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

        // Authentication
        ENABLE_QUOTA_ENFORCEMENT: 'true',

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

    const runtimeLogGroup = new logs.LogGroup(this, 'AgentCoreRuntimeLogGroup', {
      logGroupName: `/aws/bedrock-agentcore/runtimes/${config.projectPrefix}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

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
      value: runtimeLogGroup.logGroupName,
      description: 'CloudWatch Log Group for AgentCore Runtime',
      exportName: `${config.projectPrefix}-AgentCoreRuntimeLogGroup`,
    });
   }
}
