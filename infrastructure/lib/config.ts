import * as cdk from 'aws-cdk-lib';

export interface CognitoConfig {
  domainPrefix?: string;       // Custom Cognito domain prefix (defaults to projectPrefix)
  callbackUrls?: string[];     // Additional callback URLs beyond auto-derived
  logoutUrls?: string[];       // Additional logout URLs beyond auto-derived
  // Extra federated IdPs the BFF client should accept beyond the built-in
  // Cognito user directory. Names match the `ProviderName` from
  // `cognito-idp create-identity-provider` (e.g. `ms-entra-id`).
  // COGNITO is always included; entries here are added on top.
  supportedIdentityProviders?: string[];
  passwordMinLength?: number;  // Override default 8
}

export interface AppConfig {
  projectPrefix: string;
  awsAccount: string;
  awsRegion: string;
  production: boolean; // Production environment flag (default: true)
  retainDataOnDelete: boolean;
  vpcCidr: string;
  corsOrigins: string; // Top-level shared CORS origins (comma-separated), used as default for all sections
  domainName?: string; // Primary domain name for the application (used for frontend, CORS, etc.)
  infrastructureHostedZoneDomain?: string;
  // Whether CDK should create the Route53 ALIAS/A records for the SPA, ALB,
  // artifacts, and mcp-sandbox origins. Defaults to `true`. Set to `false`
  // when the hosted zone for `domainName` lives in a different AWS account
  // (or is otherwise managed out-of-band): the stack still attaches the
  // custom domain + ACM cert to every origin, but skips the in-account
  // `HostedZone.fromLookup` + record creation that would fail cross-account.
  // In that mode the deploy emits CfnOutputs with the record name + alias
  // target for each origin so an operator can create the records by hand.
  manageDnsRecords: boolean;
  albSubdomain?: string; // Subdomain for ALB (e.g., 'api' for api.yourdomain.com)
  certificateArn?: string; // ACM certificate ARN for HTTPS on the ALB (MUST be in the stack's own region)
  // Shared ACM certificate ARN for ALL CloudFront origins (SPA / artifacts /
  // mcp-sandbox). MUST be in us-east-1 (CloudFront requirement) and SHOULD be
  // a wildcard that covers both the apex/SPA domain and its subdomain origins,
  // i.e. SANs `{domainName}` AND `*.{domainName}`. When set, each CloudFront
  // section falls back to this value if its own section-specific ARN is unset.
  // A section-specific ARN (frontend/artifacts/mcpSandbox.certificateArn) always
  // wins, so an operator can override a single origin while sharing the rest.
  // The ALB cert (`certificateArn` above) is intentionally NOT covered here —
  // it lives in the stack's deploy region, not us-east-1.
  cloudfrontCertificateArn?: string;
  cognito: CognitoConfig;
  frontend: FrontendConfig;
  appApi: AppApiConfig;
  inferenceApi: InferenceApiConfig;
  ragIngestion: RagIngestionConfig;
  kbSync: KbSyncConfig;
  managedKb: ManagedKbConfig;
  scheduledRuns: ScheduledRunsConfig;
  memorySpaces: MemorySpacesConfig;
  skills: SkillsConfig;
  agents: AgentsConfig;
  agentMarketplace: AgentMarketplaceConfig;
  fineTuning: FineTuningConfig;
  artifacts: ArtifactsConfig;
  mcpSandbox: McpSandboxConfig;
  mcpIdentity: McpIdentityConfig;
  gateway: GatewayConfig;
  /**
   * Optional. Absent for any deployment without an external token service, which
   * is the default. Kept optional rather than defaulted so a fork constructing
   * AppConfig by hand does not have to know this feature exists.
   */
  tokenExchange?: TokenExchangeConfig;
  observability: ObservabilityConfig;
  appVersion: string;
  tags: { [key: string]: string };
}

/**
 * MCP Apps host renderer — sandbox-proxy origin (PR #1 of the
 * docs/kaizen/scoping/mcp-apps-host-renderer.md sequence).
 *
 * Provisions a dedicated cross-origin shell (mcp-sandbox.{domainName}) that
 * the SPA's <mcp-app-frame> is pointed at. The inference-api stack
 * consumes this stack's SSM origin export into
 * `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`. The host renderer is gated by
 * MCP_APPS_HOST_ENABLED, flipped on in PR #7.
 */
export interface McpSandboxConfig {
  // ACM certificate ARN for the proxy origin (mcp-sandbox.{domainName}).
  // MUST be in us-east-1 — CloudFront requires its viewer certs there.
  // Without it the stack still synthesizes on the CloudFront default
  // domain so unit/synth tests and domain-less local stacks work.
  certificateArn?: string;
  // Extra origins (beyond https://{domainName}) allowed to embed the proxy
  // iframe via CSP frame-ancestors — e.g. http://localhost:4200 for a local
  // SPA pointed at this deployment. Empty on prod.
  extraFrameAncestors: string[];
}

export interface ArtifactsConfig {
  // ACM certificate ARN for the artifact iframe origin (artifacts.{domainName}).
  // MUST be in us-east-1 — CloudFront requires its certs there. Validation
  // surfaces a clear error if the arn is in another region.
  certificateArn?: string;
  // Soft-delete retention window for objects tagged `lifecycle-class=deleted`.
  retentionDays: number;
  // Extra origins (beyond https://{domainName}) allowed to embed artifact
  // iframes via CSP frame-ancestors — e.g. http://localhost:4200 for a
  // local SPA pointed at this deployment. Empty on prod.
  extraFrameAncestors: string[];
}

export interface FrontendConfig {
  certificateArn?: string;
  bucketName?: string;
  cloudFrontPriceClass: string;
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
}

export interface AppApiConfig {
  cpu: number;
  memory: number;
  desiredCount: number;
  maxCapacity: number;
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
}

/**
 * Inference API config.
 *
 * The inference API runs in Bedrock AgentCore Runtime, which manages
 * its own compute. None of the typical Fargate-style knobs (cpu, memory,
 * desiredCount, maxCapacity) apply here, so they're intentionally absent.
 */
export interface InferenceApiConfig {
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
}

export interface RagIngestionConfig {
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
  lambdaMemorySize: number;      // Lambda memory in MB (default: 3008)
  lambdaTimeout: number;         // Lambda timeout in seconds (default: 900)
  embeddingModel: string;        // Bedrock model ID (default: "amazon.titan-embed-text-v2")
  vectorDimension: number;       // Embedding dimension (default: 1024)
  vectorDistanceMetric: string;  // Distance metric (default: "cosine")
}

/**
 * KB sync — scheduled re-index of assistant knowledge-base sources
 * (docs/specs/assistant-kb-sync.md).
 *
 * `enabled` gates the whole feature: it sets the EventBridge rule's
 * enabled state AND the KB_SYNC_ENABLED env var on both kb-sync
 * Lambdas. Default ON with a kill switch — the feature runs unless it's
 * explicitly turned off with CDK_KB_SYNC_ENABLED=false (or a
 * `kbSync.enabled: false` cdk.json context).
 */
export interface KbSyncConfig {
  enabled: boolean;
}

/**
 * Managed knowledge bases — Amazon Bedrock Managed KB as a second
 * retrieval backend (.kiro/specs/managed-kb-migration, Requirement 19).
 *
 * Three INDEPENDENT booleans, all defaulting to **false**, all treating
 * an empty string as false. This inverts the repo's usual "default ON
 * with a kill switch" idiom on purpose: managed storage bills at
 * $5.00/GB-month against ~$0.15/GB-month today, so every one of these
 * has to be a deliberate, reviewable opt-in rather than something a
 * fork inherits by cloning.
 *
 * The empty-string rule is not pedantry (Requirement 19.8). An unset
 * GitHub Actions variable renders as an EMPTY STRING, not as absent, so
 * a naive truthiness check on the forwarded value is fine but a naive
 * `!== 'false'` check — the shape used by the default-ON flags above —
 * would resolve an unset variable to **true** and silently arm the
 * feature on every fork. `parseBooleanEnv` returns `undefined` for both
 * unset and empty, so the `??` chain falls through to context and then
 * to `false`.
 *
 * - `newDefault` (MANAGED_KB_NEW_DEFAULT) — newly created knowledge
 *   bases are provisioned managed instead of legacy.
 * - `migrationEnabled` (MANAGED_KB_MIGRATION_ENABLED) — whether the
 *   background Migration_Worker runs at all. Off ⇒ the dispatcher's
 *   EventBridge rule is created DISABLED and the dispatcher itself
 *   no-ops (Requirement 19.6), so migration work is inert two ways
 *   over.
 * - `reconcilerArmed` (MANAGED_KB_RECONCILER_ARMED) — whether the daily
 *   Reconciler DELETES orphans or merely logs what it would have
 *   deleted. This is the inverted-convention flag: the Reconciler is
 *   *deployed and running* from day one but *disarmed*, so its
 *   judgement can be reviewed against weeks of real data before it is
 *   allowed to delete anything (Requirements 14.7, 19.7). Note the
 *   consequence for the schedule: the Reconciler's rule is ENABLED even
 *   with every flag off, because report-only is the point. Report-only
 *   is read-only, so it changes nothing.
 *
 * Byte caps (Requirement 12.2) are expressed in BYTES rather than MB so
 * nothing downstream has to guess at a unit, and they resolve by role
 * tier. The standard tier sits deliberately **below** the platform's
 * existing 1 GB user-files precedent: at 30,000 users the 1 GB
 * precedent would permit 30 TB, i.e. ~$150,000/month. These figures
 * still require product sign-off before enforcement is switched on.
 *
 * `storageAlarmGb` / `dailyCostAlarmUsd` are the fleet-level guards
 * (Requirement 12.13). Per-owner caps bound one user; only these bound
 * the account, and the gap between ~$169/month expected and ~$15,000
 * permitted is why they are not optional.
 */
export interface ManagedKbConfig {
  /** New knowledge bases are created managed. Default false. */
  newDefault: boolean;
  /** The background Migration_Worker runs at all. Default false. */
  migrationEnabled: boolean;
  /** The Reconciler deletes rather than only reporting. Default false. */
  reconcilerArmed: boolean;
  /** Per-owner Byte_Cap, standard role tier. Default 100 MB. */
  perOwnerDefaultBytes: number;
  /** Per-owner Byte_Cap, elevated (admin-granted) role tier. Default 1 GB. */
  perOwnerElevatedBytes: number;
  /** Per-knowledge-base ceiling, bounding a single runaway corpus. Default 500 MB. */
  perKnowledgeBaseCeilingBytes: number;
  /** Rollback window during `retain`, in days. At least 30 (Requirement 15.11). */
  retentionWindowDays: number;
  /** Fleet-wide managed-storage alarm threshold, in GB. */
  storageAlarmGb: number;
  /** Daily Knowledge-Base usagetype cost alarm threshold, in USD. */
  dailyCostAlarmUsd: number;
}

/** Per-owner Byte_Cap defaults by role tier (Requirement 12.2). */
export const MANAGED_KB_DEFAULT_PER_OWNER_BYTES = 100 * 1024 * 1024; // 100 MB
export const MANAGED_KB_ELEVATED_PER_OWNER_BYTES = 1024 * 1024 * 1024; // 1 GB
export const MANAGED_KB_PER_KB_CEILING_BYTES = 500 * 1024 * 1024; // 500 MB
/** Minimum legacy-data rollback window (Requirement 15.11). */
export const MANAGED_KB_RETENTION_WINDOW_DAYS = 30;

/**
 * Scheduled runs — headless agent runs as a user (the Harness primitive,
 * docs/specs/scheduled-agent-runs.md).
 *
 * `enabled` is the global kill switch: it sets the SCHEDULED_RUNS_ENABLED
 * env var on app-api (gating the "Run now" + headless-grant routes), and
 * will gate the Phase-B EventBridge dispatcher rule when the scheduler
 * lands. Default ON with a kill switch — the feature runs unless it's
 * explicitly turned off with CDK_SCHEDULED_RUNS_ENABLED=false (or a
 * `scheduledRuns.enabled: false` cdk.json context). *Who* can use the
 * surface is governed separately by the `scheduled-runs` RBAC capability.
 */
export interface ScheduledRunsConfig {
  enabled: boolean;
}

/**
 * Memory Spaces feature flag. Default ON with a kill switch — the feature is
 * complete and ships enabled for every deployer (opt-out), disabled per
 * environment with CDK_MEMORY_SPACES_ENABLED=false (or a
 * `memorySpaces.enabled: false` cdk.json context). Sets the
 * MEMORY_SPACES_ENABLED env var on app-api and inference-api. The table + bucket
 * are provisioned unconditionally, so this only gates route mounting at runtime.
 */
export interface MemorySpacesConfig {
  enabled: boolean;
}

/**
 * Skills feature flag (Skills v2). Default ON with a kill switch — the epic is
 * complete and dogfooded, so it ships enabled for every deployer (opt-out),
 * disabled per environment with CDK_SKILLS_ENABLED=false (or a
 * `skills.enabled: false` cdk.json context). Sets the SKILLS_ENABLED env var on
 * app-api and inference-api; the skills data lives in the shared app-roles
 * table, so this only gates route mounting and skill resolution at runtime.
 *
 * This gates *feature existence* per environment, and is now the only gate on
 * the user-facing surfaces: the `skills` RBAC capability that used to keep them
 * admin-only was removed (it could not be granted from the admin roles UI). Who
 * can reach *which* skills is a role's `grantedSkills`, edited in that UI.
 */
export interface SkillsConfig {
  enabled: boolean;
}

/**
 * Agent Designer feature flag. Default ON with a kill switch — the governed
 * `/agents/*` surface ships everywhere now that the Designer is complete; only
 * CDK_AGENTS_API_ENABLED=false (or an `agents.enabled: false` cdk.json context)
 * turns it off. Sets the AGENTS_API_ENABLED env var on app-api + inference-api.
 * `/assistants/*` is unaffected, and the SPA nav stays preview-gated until
 * Assistants are deprecated.
 */
export interface AgentsConfig {
  enabled: boolean;
}

/**
 * Agent Marketplace (docs/specs/agent-marketplace.md).
 *
 * Default ON with a kill switch: CDK_AGENT_MARKETPLACE_ENABLED=false (or an
 * `agentMarketplace.enabled: false` cdk.json context) turns it off. Sets the
 * AGENT_MARKETPLACE_ENABLED env var on **app-api only** — publication is a catalog
 * concern and the marketplace adds no inference-api routes.
 */
export interface AgentMarketplaceConfig {
  enabled: boolean;
}

export interface FineTuningConfig {
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
  /**
   * Mounts the `/fine-tuning` and `/admin/fine-tuning` routers in app-api.
   *
   * Distinct from the long-deleted `CDK_FINE_TUNING_ENABLED`, which gated
   * whether the SageMaker *stack* deployed and went away with the single-stack
   * migration (#396). The tables, bucket and SageMaker role are provisioned
   * unconditionally; this only decides whether the routes are reachable.
   */
  enabled: boolean;
  /**
   * Monthly GPU-hour quota granted to any authenticated user on first use.
   * `0` keeps the original whitelist-only behaviour, where an admin has to
   * grant each user explicitly.
   */
  defaultQuotaHours: number;
}

/**
 * MCP user-identity forwarding (docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md).
 *
 * Personalized MCP tools need to know *who* the logged-in user is. The only
 * token forwarded end-to-end to MCP servers is the Cognito **access token**,
 * which carries `sub` but no richer identity claims. This config gates an
 * optional Cognito Pre-Token-Generation **v2** Lambda that copies configured
 * user-pool attributes into named claims on the access token, so the existing
 * SPA -> app-api -> inference-api -> MCP forwarding path works unchanged.
 *
 * Off by default (opt-in). A fork that doesn't need personalized MCP tools
 * configures nothing: no Lambda and no pool trigger are created, and the
 * access token (with `sub`) is forwarded exactly as it is today. This inverts
 * the repo's usual "default ON with a kill switch" idiom precisely because the
 * feature has a pool-wide blast radius and an external (Cognito feature-plan)
 * dependency — it must be a deliberate opt-in.
 */
export interface McpIdentityConfig {
  // Optional Pre-Token-Generation (v2) enrichment of the access token.
  // No Lambda/trigger is created when disabled or omitted.
  // Requires the Cognito Essentials/Plus feature plan (access-token
  // customization) — the TokenEnrichmentConstruct sets the pool's
  // featurePlan to ESSENTIALS to guarantee support on fresh forks.
  tokenEnrichment?: {
    enabled: boolean;
    // {claimName: sourceCognitoAttribute}. Each present attribute is copied to
    // the named claim on the access token; missing attributes are skipped
    // (native users, or forks without that attribute). No IdP is assumed.
    // Claim names SHOULD be namespaced (full reverse-DNS form, e.g.
    // "https://example.com/employee_number") to avoid colliding with Cognito's
    // reserved access-token claims.
    accessTokenClaims?: { [claimName: string]: string };
  };
}

/**
 * AgentCore Gateway configuration.
 *
 * `inboundAuth` selects the Gateway's single inbound authorizer. AgentCore
 * allows exactly one authorizer type per Gateway (`authorizerType` is a scalar:
 * CUSTOM_JWT | AWS_IAM | NONE | AUTHENTICATE_ONLY) — there is no "accept either
 * SigV4 or JWT" mode. Outbound credentials remain per-target, so a single
 * Gateway still serves both IAM-invoked Lambda targets and OAuth/token-exchange
 * targets.
 *
 * ## The authorizer is immutable after creation
 *
 * Changing this value on an **existing** Gateway does not work. The AgentCore
 * control plane rejects it:
 *
 * ```
 * Authorizer type cannot be updated for an existing gateway
 * (Service: BedrockAgentCoreControl, Status Code: 400)
 * ```
 *
 * This is *not* visible from CloudFormation: the resource schema documents both
 * `AuthorizerType` and `AuthorizerConfiguration` as "Update requires: No
 * interruption", and `cdk diff` — even via a real change set — reports an
 * in-place `[~]` modify. Both reflect CFN's model, not the service's validation.
 * The failure only surfaces at deploy time, mid-update.
 *
 * So `inboundAuth` effectively sets the authorizer **at Gateway creation**.
 * Moving an existing deployment from AWS_IAM to CUSTOM_JWT requires a *new*
 * Gateway plus target re-registration and a cutover — not a config flip. See
 * docs/specs/AGENTCORE_GATEWAY_TOKEN_EXCHANGE_PLAN.md.
 *
 * Defaults to `iam`, matching every Gateway already deployed. Do not change the
 * default to `jwt`: it would make `PlatformStack` fail on every existing
 * deployment (this repo's included) and — because the agent reads the same
 * value — point the agent at an auth mode its Gateway does not accept.
 */
export interface GatewayConfig {
  inboundAuth: 'jwt' | 'iam';
}

/**
 * RFC 8693 token exchange against an external token service.
 *
 * Lets the agent trade the signed-in user's Cognito access token for a token
 * issued by a token service the organisation already runs, so downstream APIs
 * that trust that service can serve agent requests as the user without being
 * modified. Useful anywhere internal APIs already accept a JWT from an existing
 * identity or token service — the pattern is not specific to any one kind of
 * organisation.
 *
 * Deliberately not done by AgentCore Gateway: its outbound OAuth credential
 * provider supports only CLIENT_CREDENTIALS and AUTHORIZATION_CODE
 * (`OAuthGrantType` in the bedrock-agentcore-control API model has exactly those
 * two values), so a token-exchange grant cannot be expressed there. A Gateway
 * with AWS_IAM inbound auth also never sees the user's token, so it would have
 * nothing to exchange. The runtime performs the exchange instead.
 *
 * Entirely optional and additive. Leave it unset and no resources, permissions,
 * or environment variables are created — a deployment that only ever uses SigV4
 * for MCP traffic is unaffected.
 */
export interface TokenExchangeConfig {
  /** Exchange endpoint, e.g. https://tokens.example.org/v2/oauth/token */
  url: string;
  /** client_id this deployment authenticates as. */
  clientId: string;
}

// Observability defaults. Tuned for cost: these are what a fork inherits when it
// configures nothing. See .kiro/steering/observability.md.

/** Retention for every log group in the stack. */
export const OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS = 30;

/** X-Ray sampling rate, 0.0-1.0. Billed per trace recorded, so keep it low. */
export const OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE = 0.01;

/** Traces per second recorded before the sampling rate applies. */
export const OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RESERVOIR = 1;

/** ALB target 5xx per 5-minute period. */
export const OBSERVABILITY_DEFAULT_ALB_TARGET_5XX_THRESHOLD = 10;

/** p99 latency floor (ms). High because the chat path is SSE: a healthy turn
 *  runs for seconds and peaks around 25s, so a tight threshold only makes noise. */
export const OBSERVABILITY_DEFAULT_P99_LATENCY_MS = 120_000;

/** AgentCore Runtime errors per 5-minute period. */
export const OBSERVABILITY_DEFAULT_AGENTCORE_ERROR_THRESHOLD = 10;

/** Lambda errors per 5-minute period. */
export const OBSERVABILITY_DEFAULT_LAMBDA_ERROR_THRESHOLD = 5;

/** Lambda duration alarm as a percentage of the function's own timeout. */
export const OBSERVABILITY_DEFAULT_LAMBDA_DURATION_PERCENT_OF_TIMEOUT = 80;

/** DynamoDB throttle events per 5-minute period. */
export const OBSERVABILITY_DEFAULT_DYNAMO_THROTTLE_THRESHOLD = 10;

/** ECS service CPU / memory utilisation alarm thresholds (percent). */
export const OBSERVABILITY_DEFAULT_ECS_CPU_PERCENT = 80;
export const OBSERVABILITY_DEFAULT_ECS_MEMORY_PERCENT = 85;

/** Avoidable prompt-cache misses per 5-minute period. */
export const OBSERVABILITY_DEFAULT_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD = 10;

/** Dollars of fleet prompt-cache waste per 5-minute period. */
export const OBSERVABILITY_DEFAULT_PROMPT_CACHE_WASTED_USD_THRESHOLD = 1;

/** Cumulative partial-miss waste for one session, in dollars. A fleet sum
 *  cannot see a single conversation re-writing its prefix every turn. */
export const OBSERVABILITY_DEFAULT_PROMPT_CACHE_SESSION_WASTED_USD_THRESHOLD = 5;

/**
 * Observability configuration.
 *
 * Precedence per field: CDK_OBSERVABILITY_* env var, then the flat dotted
 * context key, then a nested `observability` object, then the default constant.
 */
export interface ObservabilityConfig {
  /** Create the SNS alarm topic and route every alarm to it. */
  alarmTopicEnabled: boolean;
  logRetentionDays: number;
  albTarget5xxThreshold: number;
  /** ALB p99 TargetResponseTime floor, in ms. */
  albP99LatencyMs: number;
  /** AgentCore Runtime p99 Latency floor, in ms. */
  agentCoreLatencyMs: number;
  agentCoreErrorThreshold: number;
  lambdaErrorThreshold: number;
  lambdaDurationPercentOfTimeout: number;
  dynamoThrottleThreshold: number;
  ecsCpuPercent: number;
  ecsMemoryPercent: number;

  promptCacheAvoidableMissThreshold: number;
  promptCacheWastedUsdThreshold: number;
  promptCacheSessionWastedUsdThreshold: number;

  xraySamplingRate: number;
  xraySamplingReservoir: number;
  xrayInsightsNotifications: boolean;
  /** AgentCore APPLICATION_LOGS vended delivery. Off by default: the records
   *  carry full prompts and responses, so it is both high-volume and PII. */
  agentCoreApplicationLogsEnabled: boolean;
}

/**
 * Load and validate configuration from CDK context
 * @param scope The CDK construct scope
 * @returns Validated AppConfig object
 */
export function loadConfig(scope: cdk.App): AppConfig {
  // Load required configuration from environment variables or context
  const projectPrefix = process.env.CDK_PROJECT_PREFIX || scope.node.tryGetContext('projectPrefix');
  const tokenExchangeUrl =
    process.env.CDK_TOKEN_EXCHANGE_URL
    || scope.node.tryGetContext('tokenExchange')?.url
    || '';
  const tokenExchangeClientId =
    process.env.CDK_TOKEN_EXCHANGE_CLIENT_ID
    || scope.node.tryGetContext('tokenExchange')?.clientId
    || '';
  const awsRegion = process.env.CDK_AWS_REGION || scope.node.tryGetContext('awsRegion');
  
  // Validate required variables
  if (!projectPrefix) {
    throw new Error(
      'CDK_PROJECT_PREFIX is required. ' +
      'Set this environment variable to your desired resource name prefix ' +
      '(e.g., "mycompany-agentcore" or "mycompany-agentcore-prod")'
    );
  }
  
  if (!awsRegion) {
    throw new Error(
      'CDK_AWS_REGION is required. ' +
      'Set this environment variable to your target AWS region ' +
      '(e.g., "us-east-1", "us-west-2", "eu-west-1")'
    );
  }
  
  // AWS Account can come from environment variable or context
  const awsAccount = process.env.CDK_AWS_ACCOUNT ||
                     scope.node.tryGetContext('awsAccount') || 
                     process.env.CDK_DEFAULT_ACCOUNT ||
                     process.env.AWS_ACCOUNT_ID;
  
  if (!awsAccount) {
    throw new Error(
      'CDK_AWS_ACCOUNT is required. ' +
      'Set this environment variable to your AWS account ID ' +
      '(e.g., "123456789012")'
    );
  }

  // Validate AWS account and region
  validateAwsAccount(awsAccount);
  validateAwsRegion(awsRegion);

  // Top-level shared CORS origins — always includes https://{domainName} when set.
  // CDK_CORS_ORIGINS provides ADDITIONAL origins on top of the domain.
  const domainName = process.env.CDK_DOMAIN_NAME || scope.node.tryGetContext('domainName');
  const extraCorsOrigins = process.env.CDK_CORS_ORIGINS
    || scope.node.tryGetContext('corsOrigins')
    || '';
  // Build corsOrigins: domain-derived origin first, then any extras
  const corsOriginParts: string[] = [];
  if (domainName) {
    corsOriginParts.push(`https://${domainName}`);
  }
  if (extraCorsOrigins) {
    corsOriginParts.push(extraCorsOrigins);
  }
  const corsOrigins = corsOriginParts.join(',');

  // Load app version from environment variable or CDK context
  const appVersion = process.env.CDK_APP_VERSION || scope.node.tryGetContext('appVersion') || 'unknown';

  const config: AppConfig = {
    projectPrefix,
    appVersion,
    awsAccount,
    awsRegion,
    production: parseBooleanEnv(process.env.CDK_PRODUCTION) ?? scope.node.tryGetContext('production'),
    retainDataOnDelete: parseBooleanEnv(process.env.CDK_RETAIN_DATA_ON_DELETE) ?? scope.node.tryGetContext('retainDataOnDelete'),
    vpcCidr: scope.node.tryGetContext('vpcCidr'),
    corsOrigins,
    domainName,
    infrastructureHostedZoneDomain: process.env.CDK_HOSTED_ZONE_DOMAIN || scope.node.tryGetContext('infrastructureHostedZoneDomain'),
    manageDnsRecords: parseBooleanEnv(process.env.CDK_MANAGE_DNS_RECORDS)
      ?? scope.node.tryGetContext('manageDnsRecords')
      ?? true,
    albSubdomain: process.env.CDK_ALB_SUBDOMAIN || scope.node.tryGetContext('albSubdomain'),
    certificateArn: process.env.CDK_CERTIFICATE_ARN || scope.node.tryGetContext('certificateArn'),
    cloudfrontCertificateArn: process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN || scope.node.tryGetContext('cloudfrontCertificateArn'),
    cognito: {
      domainPrefix: process.env.CDK_COGNITO_DOMAIN_PREFIX
        || scope.node.tryGetContext('cognito')?.domainPrefix
        || projectPrefix,
      callbackUrls: process.env.CDK_COGNITO_CALLBACK_URLS?.split(',')
        .map((s) => s.trim()).filter(Boolean)
        || scope.node.tryGetContext('cognito')?.callbackUrls,
      logoutUrls: process.env.CDK_COGNITO_LOGOUT_URLS?.split(',')
        .map((s) => s.trim()).filter(Boolean)
        || scope.node.tryGetContext('cognito')?.logoutUrls,
      supportedIdentityProviders: process.env.CDK_COGNITO_SUPPORTED_IDPS?.split(',')
        .map((s) => s.trim()).filter(Boolean)
        || scope.node.tryGetContext('cognito')?.supportedIdentityProviders,
      passwordMinLength: parseIntEnv(process.env.CDK_COGNITO_PASSWORD_MIN_LENGTH)
        || scope.node.tryGetContext('cognito')?.passwordMinLength
        || 8,
    },
    frontend: {
      certificateArn: process.env.CDK_FRONTEND_CERTIFICATE_ARN || scope.node.tryGetContext('frontend').certificateArn,
      bucketName: process.env.CDK_FRONTEND_BUCKET_NAME || scope.node.tryGetContext('frontend')?.bucketName,
      cloudFrontPriceClass: process.env.CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS || scope.node.tryGetContext('frontend')?.cloudFrontPriceClass,
      additionalCorsOrigins: process.env.CDK_FRONTEND_CORS_ORIGINS || scope.node.tryGetContext('frontend')?.additionalCorsOrigins,
    },
    appApi: {
      cpu: parseIntEnv(process.env.CDK_APP_API_CPU) || scope.node.tryGetContext('appApi')?.cpu,
      memory: parseIntEnv(process.env.CDK_APP_API_MEMORY) || scope.node.tryGetContext('appApi')?.memory,
      desiredCount: parseIntEnv(process.env.CDK_APP_API_DESIRED_COUNT) ?? scope.node.tryGetContext('appApi')?.desiredCount,
      maxCapacity: parseIntEnv(process.env.CDK_APP_API_MAX_CAPACITY) || scope.node.tryGetContext('appApi')?.maxCapacity,
      additionalCorsOrigins: process.env.CDK_APP_API_CORS_ORIGINS || scope.node.tryGetContext('appApi')?.additionalCorsOrigins,
    },
    inferenceApi: {
      additionalCorsOrigins: process.env.CDK_INFERENCE_API_CORS_ORIGINS || scope.node.tryGetContext('inferenceApi')?.additionalCorsOrigins,
    },
    ragIngestion: {
      additionalCorsOrigins: process.env.CDK_RAG_CORS_ORIGINS || scope.node.tryGetContext('ragIngestion')?.additionalCorsOrigins,
      lambdaMemorySize: parseIntEnv(process.env.CDK_RAG_LAMBDA_MEMORY) || scope.node.tryGetContext('ragIngestion')?.lambdaMemorySize,
      lambdaTimeout: parseIntEnv(process.env.CDK_RAG_LAMBDA_TIMEOUT) || scope.node.tryGetContext('ragIngestion')?.lambdaTimeout,
      embeddingModel: process.env.CDK_RAG_EMBEDDING_MODEL || scope.node.tryGetContext('ragIngestion')?.embeddingModel,
      vectorDimension: parseIntEnv(process.env.CDK_RAG_VECTOR_DIMENSION) || scope.node.tryGetContext('ragIngestion')?.vectorDimension,
      vectorDistanceMetric: process.env.CDK_RAG_DISTANCE_METRIC || scope.node.tryGetContext('ragIngestion')?.vectorDistanceMetric,
    },
    kbSync: {
      // Default ON with a kill switch: enabled unless explicitly disabled.
      // The workflow forwards `${{ vars.CDK_KB_SYNC_ENABLED }}`, which is an
      // EMPTY STRING when the variable is unset — so treat empty/unset as
      // "use the default (on)" and only the literal "false" as the off
      // switch. A `kbSync.enabled` cdk.json context can also force it off.
      enabled: process.env.CDK_KB_SYNC_ENABLED
        ? process.env.CDK_KB_SYNC_ENABLED !== 'false'
        : scope.node.tryGetContext('kbSync')?.enabled ?? true,
    },
    // Managed knowledge bases (.kiro/specs/managed-kb-migration).
    //
    // OPT-IN, inverting the default-ON idiom used by the flags above and
    // below, because managed storage costs ~35x legacy per GB-month. All
    // three booleans default to FALSE and an EMPTY STRING resolves to
    // false (Requirement 19.8): `parseBooleanEnv` returns undefined for
    // both unset and empty — which is exactly what an unset GitHub
    // Actions variable forwards — so the `??` chain falls through to
    // cdk.json context and then to the `false` literal. Deliberately NOT
    // the `X ? X !== 'false' : default` shape used by kbSync /
    // scheduledRuns / skills: that shape reads an unset (empty-string)
    // variable as the default, which is correct when the default is ON
    // and catastrophic when it is OFF.
    //
    // Precedence, highest first:
    //   1. CDK_MANAGED_KB_* environment variable
    //   2. `--context managedKb.<flag>=...` from build_cdk_context_params
    //   3. a nested `managedKb: { ... }` object in cdk.context.json
    //   4. false
    //
    // Step 2 reads the FLAT dotted key on purpose. `--context a.b=c` sets
    // context["a.b"], it does NOT build a nested object — verified
    // empirically: with `--context probe.flag=true`,
    // tryGetContext('probe.flag') is the string "true" while
    // tryGetContext('probe') is undefined. So a section that reads only
    // `tryGetContext('managedKb')?.flag` silently ignores its own
    // --context flag, which is the state the sibling dotted flags in
    // load-env.sh are in. Reading both keys is what makes the documented
    // GitHub-variable → workflow → load-env → synth/deploy → config
    // chain actually deliver a value. Do not "simplify" this away.
    managedKb: {
      newDefault:
        parseBooleanEnv(process.env.CDK_MANAGED_KB_NEW_DEFAULT)
        ?? parseBooleanEnv(scope.node.tryGetContext('managedKb.newDefault'))
        ?? scope.node.tryGetContext('managedKb')?.newDefault
        ?? false,
      migrationEnabled:
        parseBooleanEnv(process.env.CDK_MANAGED_KB_MIGRATION_ENABLED)
        ?? parseBooleanEnv(scope.node.tryGetContext('managedKb.migrationEnabled'))
        ?? scope.node.tryGetContext('managedKb')?.migrationEnabled
        ?? false,
      reconcilerArmed:
        parseBooleanEnv(process.env.CDK_MANAGED_KB_RECONCILER_ARMED)
        ?? parseBooleanEnv(scope.node.tryGetContext('managedKb.reconcilerArmed'))
        ?? scope.node.tryGetContext('managedKb')?.reconcilerArmed
        ?? false,
      // Byte caps in BYTES so no consumer has to guess a unit. The
      // standard tier is deliberately below the 1 GB user-files
      // precedent (Requirement 12.2).
      //
      // Same three-step precedence as the flags above, and for the same
      // reason: step 2 reads the FLAT dotted key that
      // `--context managedKb.perOwnerDefaultBytes=...` actually sets.
      // Without it, load-env.sh's --context flag for these tunables
      // would be accepted by the CLI and then silently ignored.
      perOwnerDefaultBytes:
        parseIntEnv(process.env.CDK_MANAGED_KB_PER_OWNER_BYTES)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.perOwnerDefaultBytes'))
        ?? scope.node.tryGetContext('managedKb')?.perOwnerDefaultBytes
        ?? MANAGED_KB_DEFAULT_PER_OWNER_BYTES,
      perOwnerElevatedBytes:
        parseIntEnv(process.env.CDK_MANAGED_KB_PER_OWNER_ELEVATED_BYTES)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.perOwnerElevatedBytes'))
        ?? scope.node.tryGetContext('managedKb')?.perOwnerElevatedBytes
        ?? MANAGED_KB_ELEVATED_PER_OWNER_BYTES,
      perKnowledgeBaseCeilingBytes:
        parseIntEnv(process.env.CDK_MANAGED_KB_PER_KB_CEILING_BYTES)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.perKnowledgeBaseCeilingBytes'))
        ?? scope.node.tryGetContext('managedKb')?.perKnowledgeBaseCeilingBytes
        ?? MANAGED_KB_PER_KB_CEILING_BYTES,
      retentionWindowDays:
        parseIntEnv(process.env.CDK_MANAGED_KB_RETENTION_WINDOW_DAYS)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.retentionWindowDays'))
        ?? scope.node.tryGetContext('managedKb')?.retentionWindowDays
        ?? MANAGED_KB_RETENTION_WINDOW_DAYS,
      // Fleet-level alarm thresholds (Requirement 12.13). Same three-step
      // precedence as everything above, INCLUDING the flat dotted read —
      // load-env.sh emits `--context managedKb.storageAlarmGb=...` and
      // `--context managedKb.dailyCostAlarmUsd=...`, which set the flat
      // keys `context['managedKb.storageAlarmGb']` /
      // `context['managedKb.dailyCostAlarmUsd']`. Omitting the dotted read
      // makes the CLI accept those flags and then silently ignore them, so
      // an operator who raises a threshold via context keeps the old one
      // and only finds out when an alarm fires at the wrong number.
      storageAlarmGb:
        parseIntEnv(process.env.CDK_MANAGED_KB_STORAGE_ALARM_GB)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.storageAlarmGb'))
        ?? scope.node.tryGetContext('managedKb')?.storageAlarmGb
        ?? 500,
      dailyCostAlarmUsd:
        parseIntEnv(process.env.CDK_MANAGED_KB_DAILY_COST_ALARM_USD)
        ?? parseIntEnv(scope.node.tryGetContext('managedKb.dailyCostAlarmUsd'))
        ?? scope.node.tryGetContext('managedKb')?.dailyCostAlarmUsd
        ?? 100,
    },
    scheduledRuns: {
      // Default ON with a kill switch: enabled unless explicitly disabled.
      // The workflow forwards `${{ vars.CDK_SCHEDULED_RUNS_ENABLED }}`,
      // which is an EMPTY STRING when the variable is unset — so treat
      // empty/unset as "use the default (on)" and only the literal "false"
      // as the off switch. A `scheduledRuns.enabled` cdk.json context can
      // also force it off. (Same ternary as kbSync above — keep in sync.)
      enabled: process.env.CDK_SCHEDULED_RUNS_ENABLED
        ? process.env.CDK_SCHEDULED_RUNS_ENABLED !== 'false'
        : scope.node.tryGetContext('scheduledRuns')?.enabled ?? true,
    },
    memorySpaces: {
      // Default ON with a kill switch: Memory Spaces is a complete feature and
      // ships enabled for every deployer (opt-out, not opt-in — matches kbSync /
      // scheduledRuns). The table + bucket are provisioned unconditionally, so this
      // only toggles the runtime MEMORY_SPACES_ENABLED env var. The workflow forwards
      // an EMPTY STRING when the variable is unset, so treat empty/unset as the
      // default (on) and only the literal "false" as the kill switch. A
      // `memorySpaces.enabled: false` cdk.json context can also disable it.
      enabled: process.env.CDK_MEMORY_SPACES_ENABLED
        ? process.env.CDK_MEMORY_SPACES_ENABLED !== 'false'
        : scope.node.tryGetContext('memorySpaces')?.enabled ?? true,
    },
    skills: {
      // Default ON with a kill switch (house style, mirroring memorySpaces /
      // scheduledRuns): the workflow forwards an EMPTY STRING when the variable is
      // unset, so treat empty/unset as the default (on) and only the literal
      // "false" as the kill switch. A `skills.enabled: false` cdk.json context can
      // also force it off. Skills v2 is complete and dogfooded end to end, so it
      // ships on everywhere; the user-facing surfaces stay admin-only via the
      // separate `skills` RBAC capability until GA.
      enabled: process.env.CDK_SKILLS_ENABLED
        ? process.env.CDK_SKILLS_ENABLED !== 'false'
        : scope.node.tryGetContext('skills')?.enabled ?? true,
    },
    agents: {
      // Default ON with a kill switch (house style, mirroring memorySpaces /
      // scheduledRuns): the workflow forwards an EMPTY STRING when unset, so treat
      // empty/unset as the default (on) and only the literal "false" as the off
      // switch. An `agents.enabled` cdk.json context can also force it off. The
      // Agent Designer is complete, so it ships on everywhere; the SPA nav stays
      // preview-gated (system-admin) until Assistants are deprecated.
      enabled: process.env.CDK_AGENTS_API_ENABLED
        ? process.env.CDK_AGENTS_API_ENABLED !== 'false'
        : scope.node.tryGetContext('agents')?.enabled ?? true,
    },
    agentMarketplace: {
      // Default ON with a kill switch, same empty-string-safe ternary as `agents`
      // above: the workflow forwards an EMPTY STRING when the variable is unset, so
      // treat empty/unset as the default (on) and only the literal "false" as off.
      // Phase 1 ships nothing user-visible — the author submit routes and the admin
      // Review queue / Listings pages — so it is safe on everywhere from the start.
      enabled: process.env.CDK_AGENT_MARKETPLACE_ENABLED
        ? process.env.CDK_AGENT_MARKETPLACE_ENABLED !== 'false'
        : scope.node.tryGetContext('agentMarketplace')?.enabled ?? true,
    },
    fineTuning: {
      additionalCorsOrigins: process.env.CDK_FINE_TUNING_CORS_ORIGINS || scope.node.tryGetContext('fineTuning')?.additionalCorsOrigins,
      // Default ON with a kill switch, same empty-string-safe ternary as
      // `agentMarketplace` above: the workflow forwards an EMPTY STRING when the
      // variable is unset, so treat empty/unset as the default (on) and only the
      // literal "false" as off.
      enabled: process.env.CDK_FINE_TUNING_ENABLED
        ? process.env.CDK_FINE_TUNING_ENABLED !== 'false'
        : scope.node.tryGetContext('fineTuning')?.enabled ?? true,
      defaultQuotaHours:
        parseIntEnv(process.env.CDK_FINE_TUNING_DEFAULT_QUOTA_HOURS)
        ?? scope.node.tryGetContext('fineTuning')?.defaultQuotaHours
        ?? 0,
    },
    artifacts: {
      certificateArn: process.env.CDK_ARTIFACTS_CERTIFICATE_ARN || scope.node.tryGetContext('artifacts')?.certificateArn,
      retentionDays: parseIntEnv(process.env.CDK_ARTIFACTS_RETENTION_DAYS) ?? scope.node.tryGetContext('artifacts')?.retentionDays ?? 90,
      extraFrameAncestors: process.env.CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS?.split(',')
        .map((s) => s.trim()).filter(Boolean)
        || scope.node.tryGetContext('artifacts')?.extraFrameAncestors
        || [],
    },
    mcpSandbox: {
      certificateArn: process.env.CDK_MCP_SANDBOX_CERTIFICATE_ARN || scope.node.tryGetContext('mcpSandbox')?.certificateArn,
      extraFrameAncestors: process.env.CDK_MCP_SANDBOX_EXTRA_FRAME_ANCESTORS?.split(',')
        .map((s) => s.trim()).filter(Boolean)
        || scope.node.tryGetContext('mcpSandbox')?.extraFrameAncestors
        || [],
    },
    mcpIdentity: {
      // Off by default (opt-in). Env wins over context; context wins over the
      // false default. `parseBooleanEnv` returns undefined for unset/empty
      // (including the empty string an unset GitHub Actions variable renders
      // to), so `??` falls through to context, then to `false`.
      tokenEnrichment: {
        enabled:
          parseBooleanEnv(process.env.CDK_MCP_TOKEN_ENRICHMENT_ENABLED)
          ?? scope.node.tryGetContext('mcpIdentity')?.tokenEnrichment?.enabled
          ?? false,
        // {claimName: sourceCognitoAttribute}. Settable as a JSON object via
        // the CDK_MCP_TOKEN_ENRICHMENT_CLAIMS env var (so a fork can enable the
        // feature entirely through GitHub Actions variables while the public
        // cdk.context.json stays inert), or via context. Env wins over context;
        // both default to an empty map (no claims copied).
        accessTokenClaims:
          parseJsonRecordEnv(process.env.CDK_MCP_TOKEN_ENRICHMENT_CLAIMS)
          ?? scope.node.tryGetContext('mcpIdentity')?.tokenEnrichment?.accessTokenClaims
          ?? {},
      },
    },
    gateway: {
      // Inbound authorizer selection. Defaults to 'iam' — the authorizer is
      // immutable after Gateway creation (the AgentCore control plane rejects
      // an authorizerType change), so every already-deployed Gateway is
      // AWS_IAM and the default must match that. 'jwt' applies to a *newly
      // created* Gateway. See GatewayConfig.
      inboundAuth:
        (process.env.CDK_GATEWAY_INBOUND_AUTH as 'jwt' | 'iam' | undefined)
        || scope.node.tryGetContext('gateway')?.inboundAuth
        || 'iam',
    },
    // Left undefined unless a URL is configured, so the feature and every
    // resource behind it stay absent for deployments that do not want it.
    tokenExchange: tokenExchangeUrl
      ? {
          url: tokenExchangeUrl,
          clientId: tokenExchangeClientId,
        }
      : undefined,
    // Same precedence as managedKb above. The flat dotted read at step 2 is
    // load-bearing: `--context observability.x=y` sets context['observability.x'],
    // it does NOT build a nested object.
    observability: {
      alarmTopicEnabled:
        parseBooleanEnv(process.env.CDK_OBSERVABILITY_ALARM_TOPIC_ENABLED)
        ?? parseBooleanEnv(scope.node.tryGetContext('observability.alarmTopicEnabled'))
        ?? scope.node.tryGetContext('observability')?.alarmTopicEnabled
        ?? true,
      logRetentionDays:
        parseIntEnv(process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS)
        ?? parseIntEnv(scope.node.tryGetContext('observability.logRetentionDays'))
        ?? scope.node.tryGetContext('observability')?.logRetentionDays
        ?? OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS,
      albTarget5xxThreshold:
        parseIntEnv(process.env.CDK_OBSERVABILITY_ALB_TARGET_5XX_THRESHOLD)
        ?? parseIntEnv(scope.node.tryGetContext('observability.albTarget5xxThreshold'))
        ?? scope.node.tryGetContext('observability')?.albTarget5xxThreshold
        ?? OBSERVABILITY_DEFAULT_ALB_TARGET_5XX_THRESHOLD,
      albP99LatencyMs:
        parseIntEnv(process.env.CDK_OBSERVABILITY_ALB_P99_LATENCY_MS)
        ?? parseIntEnv(scope.node.tryGetContext('observability.albP99LatencyMs'))
        ?? scope.node.tryGetContext('observability')?.albP99LatencyMs
        ?? OBSERVABILITY_DEFAULT_P99_LATENCY_MS,
      agentCoreLatencyMs:
        parseIntEnv(process.env.CDK_OBSERVABILITY_AGENTCORE_LATENCY_MS)
        ?? parseIntEnv(scope.node.tryGetContext('observability.agentCoreLatencyMs'))
        ?? scope.node.tryGetContext('observability')?.agentCoreLatencyMs
        ?? OBSERVABILITY_DEFAULT_P99_LATENCY_MS,
      agentCoreErrorThreshold:
        parseIntEnv(process.env.CDK_OBSERVABILITY_AGENTCORE_ERROR_THRESHOLD)
        ?? parseIntEnv(scope.node.tryGetContext('observability.agentCoreErrorThreshold'))
        ?? scope.node.tryGetContext('observability')?.agentCoreErrorThreshold
        ?? OBSERVABILITY_DEFAULT_AGENTCORE_ERROR_THRESHOLD,
      lambdaErrorThreshold:
        parseIntEnv(process.env.CDK_OBSERVABILITY_LAMBDA_ERROR_THRESHOLD)
        ?? parseIntEnv(scope.node.tryGetContext('observability.lambdaErrorThreshold'))
        ?? scope.node.tryGetContext('observability')?.lambdaErrorThreshold
        ?? OBSERVABILITY_DEFAULT_LAMBDA_ERROR_THRESHOLD,
      lambdaDurationPercentOfTimeout:
        parseIntEnv(process.env.CDK_OBSERVABILITY_LAMBDA_DURATION_PERCENT_OF_TIMEOUT)
        ?? parseIntEnv(scope.node.tryGetContext('observability.lambdaDurationPercentOfTimeout'))
        ?? scope.node.tryGetContext('observability')?.lambdaDurationPercentOfTimeout
        ?? OBSERVABILITY_DEFAULT_LAMBDA_DURATION_PERCENT_OF_TIMEOUT,
      dynamoThrottleThreshold:
        parseIntEnv(process.env.CDK_OBSERVABILITY_DYNAMO_THROTTLE_THRESHOLD)
        ?? parseIntEnv(scope.node.tryGetContext('observability.dynamoThrottleThreshold'))
        ?? scope.node.tryGetContext('observability')?.dynamoThrottleThreshold
        ?? OBSERVABILITY_DEFAULT_DYNAMO_THROTTLE_THRESHOLD,
      ecsCpuPercent:
        parseIntEnv(process.env.CDK_OBSERVABILITY_ECS_CPU_PERCENT)
        ?? parseIntEnv(scope.node.tryGetContext('observability.ecsCpuPercent'))
        ?? scope.node.tryGetContext('observability')?.ecsCpuPercent
        ?? OBSERVABILITY_DEFAULT_ECS_CPU_PERCENT,
      ecsMemoryPercent:
        parseIntEnv(process.env.CDK_OBSERVABILITY_ECS_MEMORY_PERCENT)
        ?? parseIntEnv(scope.node.tryGetContext('observability.ecsMemoryPercent'))
        ?? scope.node.tryGetContext('observability')?.ecsMemoryPercent
        ?? OBSERVABILITY_DEFAULT_ECS_MEMORY_PERCENT,
      promptCacheAvoidableMissThreshold:
        parseIntEnv(process.env.CDK_OBSERVABILITY_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD)
        ?? parseIntEnv(scope.node.tryGetContext('observability.promptCacheAvoidableMissThreshold'))
        ?? scope.node.tryGetContext('observability')?.promptCacheAvoidableMissThreshold
        ?? OBSERVABILITY_DEFAULT_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD,
      promptCacheWastedUsdThreshold:
        parseFloatEnv(process.env.CDK_OBSERVABILITY_PROMPT_CACHE_WASTED_USD_THRESHOLD)
        ?? parseFloatEnv(scope.node.tryGetContext('observability.promptCacheWastedUsdThreshold'))
        ?? scope.node.tryGetContext('observability')?.promptCacheWastedUsdThreshold
        ?? OBSERVABILITY_DEFAULT_PROMPT_CACHE_WASTED_USD_THRESHOLD,
      promptCacheSessionWastedUsdThreshold:
        parseFloatEnv(process.env.CDK_OBSERVABILITY_PROMPT_CACHE_SESSION_WASTED_USD_THRESHOLD)
        ?? parseFloatEnv(scope.node.tryGetContext('observability.promptCacheSessionWastedUsdThreshold'))
        ?? scope.node.tryGetContext('observability')?.promptCacheSessionWastedUsdThreshold
        ?? OBSERVABILITY_DEFAULT_PROMPT_CACHE_SESSION_WASTED_USD_THRESHOLD,
      // parseFloatEnv: parseIntEnv turns 0.05 into 0, disabling sampling.
      xraySamplingRate:
        parseFloatEnv(process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE)
        ?? parseFloatEnv(scope.node.tryGetContext('observability.xraySamplingRate'))
        ?? scope.node.tryGetContext('observability')?.xraySamplingRate
        ?? OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE,
      xraySamplingReservoir:
        parseIntEnv(process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RESERVOIR)
        ?? parseIntEnv(scope.node.tryGetContext('observability.xraySamplingReservoir'))
        ?? scope.node.tryGetContext('observability')?.xraySamplingReservoir
        ?? OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RESERVOIR,
      xrayInsightsNotifications:
        parseBooleanEnv(process.env.CDK_OBSERVABILITY_XRAY_INSIGHTS_NOTIFICATIONS)
        ?? parseBooleanEnv(scope.node.tryGetContext('observability.xrayInsightsNotifications'))
        ?? scope.node.tryGetContext('observability')?.xrayInsightsNotifications
        ?? false,
      agentCoreApplicationLogsEnabled:
        parseBooleanEnv(process.env.CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED)
        ?? parseBooleanEnv(scope.node.tryGetContext('observability.agentCoreApplicationLogsEnabled'))
        ?? scope.node.tryGetContext('observability')?.agentCoreApplicationLogsEnabled
        ?? false,
    },
    tags: {
      ...(scope.node.tryGetContext('tags') || {}),
      // `--context tags.Environment=dev` sets the FLAT dotted key
      // `context['tags.Environment']`; it does NOT merge into the nested `tags`
      // object above, so a nested-only read silently ignores an operator's own
      // flag. That trap has already bitten this repo twice (the managed-KB byte
      // caps and then the alarm thresholds), and here it had a sharper edge: the
      // Environment tag is a *filter* for the reconciler and for teardown, so
      // ignoring it does not degrade cosmetically — it makes teardown match
      // nothing and report success.
      ...(scope.node.tryGetContext('tags.Environment')
        ? { Environment: String(scope.node.tryGetContext('tags.Environment')) }
        : {}),
    },
  };

  // Resolve the shared CloudFront certificate fallback. A single wildcard
  // cert in us-east-1 (SANs `{domainName}` + `*.{domainName}`) can terminate
  // TLS for all three CloudFront origins — the SPA (`{domainName}`), the
  // artifacts iframe (`artifacts.{domainName}`), and the MCP sandbox proxy
  // (`mcp-sandbox.{domainName}`). Operators that supply one
  // CDK_CLOUDFRONT_CERTIFICATE_ARN therefore satisfy every origin at once,
  // instead of having to mint and wire three separate ARNs (the first-deploy
  // footgun this collapses). A section-specific ARN still wins, so a single
  // origin can be overridden while the rest share the wildcard.
  if (config.cloudfrontCertificateArn) {
    config.frontend.certificateArn =
      config.frontend.certificateArn || config.cloudfrontCertificateArn;
    config.artifacts.certificateArn =
      config.artifacts.certificateArn || config.cloudfrontCertificateArn;
    config.mcpSandbox.certificateArn =
      config.mcpSandbox.certificateArn || config.cloudfrontCertificateArn;
  }

  // Log loaded configuration for debugging
  console.log('📋 Loaded CDK Configuration:');
  console.log(`   Project Prefix: ${config.projectPrefix}`);
  console.log(`   AWS Region: ${config.awsRegion}`);
  console.log(`   Production: ${config.production}`);
  console.log(`   Retain Data on Delete: ${config.retainDataOnDelete}`);
  console.log(`   Manage DNS Records: ${config.manageDnsRecords}`);
  console.log(`   App Version: ${config.appVersion}`);
  // Printed so a deploy log shows which values actually took effect.
  console.log(
    `   Observability: alarmTopic=${config.observability.alarmTopicEnabled}`
    + ` logRetentionDays=${config.observability.logRetentionDays}`
    + ` xraySamplingRate=${config.observability.xraySamplingRate}`
    + ` xrayReservoir=${config.observability.xraySamplingReservoir}`
    + ` agentCoreAppLogs=${config.observability.agentCoreApplicationLogsEnabled}`
  );

  // Validate configuration
  validateConfig(config);

  return config;
}

/**
 * Parse boolean environment variable with validation.
 * 
 * When called WITHOUT a defaultValue, returns undefined for missing/empty
 * env vars so that nullish coalescing (??) can fall through to context defaults.
 * When called WITH a defaultValue, returns that default for missing/empty env vars.
 * 
 * @param value The environment variable value to parse
 * @param defaultValue Optional default when env var is not set
 * @returns The parsed boolean, or undefined if unset and no default provided
 * @throws Error if the value is present but invalid
 */
export function parseBooleanEnv(value: string | undefined): boolean | undefined;
export function parseBooleanEnv(value: string | undefined, defaultValue: boolean): boolean;
export function parseBooleanEnv(value: string | undefined, defaultValue?: boolean): boolean | undefined {
  if (value === undefined || value === '') {
    return defaultValue;
  }

  const normalized = value.toLowerCase();
  if (normalized === 'true' || normalized === '1') {
    return true;
  }
  if (normalized === 'false' || normalized === '0') {
    return false;
  }

  throw new Error(
    `Invalid boolean value: "${value}". ` +
    `Expected "true", "false", "1", or "0".`
  );
}

/**
 * Parse integer environment variable
 * Returns undefined if the value is not set or invalid, allowing for fallback logic
 */
function parseIntEnv(value: string | undefined): number | undefined {
  if (value === undefined || value === '') {
    return undefined;
  }
  const parsed = parseInt(value, 10);
  return isNaN(parsed) ? undefined : parsed;
}

/**
 * Parse a floating-point environment/context value.
 *
 * Separate from parseIntEnv because the fractional observability tunables
 * (notably the X-Ray sampling rate) round to 0 under parseInt — "0.05" would
 * become 0 and switch sampling off entirely rather than setting it to 5%.
 * Returns undefined for unset/empty/invalid input so nullish coalescing can
 * fall through to a context value or default.
 */
function parseFloatEnv(value: string | undefined): number | undefined {
  if (value === undefined || value === '') {
    return undefined;
  }
  const parsed = parseFloat(value);
  return isNaN(parsed) ? undefined : parsed;
}

/**
 * Parse a JSON object of string->string from an environment variable.
 *
 * Used for structured config that can't be expressed as a scalar `--context`
 * value (e.g. the MCP token-enrichment claim map). Returns undefined for
 * unset/empty/invalid input so nullish coalescing (??) can fall through to a
 * context value or default. Non-object JSON, or entries whose key/value aren't
 * both strings, are rejected/filtered — a malformed value must never crash the
 * synth, it simply falls through.
 */
export function parseJsonRecordEnv(
  value: string | undefined,
): { [key: string]: string } | undefined {
  if (value === undefined || value.trim() === '') {
    return undefined;
  }
  try {
    const parsed = JSON.parse(value);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return undefined;
    }
    const result: { [key: string]: string } = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof k === 'string' && typeof v === 'string') {
        result[k] = v;
      }
    }
    return result;
  } catch {
    return undefined;
  }
}

/**
 * Validate AWS account ID format
 * @param account The AWS account ID to validate
 * @throws Error if the account ID is invalid
 */
export function validateAwsAccount(account: string): void {
  if (!/^\d{12}$/.test(account)) {
    throw new Error(
      `Invalid AWS account ID: "${account}". ` +
      `Expected a 12-digit number.`
    );
  }
}

/**
 * Validate AWS region code
 * @param region The AWS region to validate
 * @throws Error if the region is invalid
 */
export function validateAwsRegion(region: string): void {
  const validRegions = [
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'ca-central-1',
    'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1', 'eu-north-1',
    'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
    'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3',
    'ap-south-1', 'ap-east-1',
    'sa-east-1',
    'me-south-1',
    'af-south-1',
  ];
  
  if (!validRegions.includes(region)) {
    throw new Error(
      `Invalid AWS region: "${region}". ` +
      `Expected one of: ${validRegions.join(', ')}`
    );
  }
}

/**
 * Validate configuration values
 */
function validateConfig(config: AppConfig): void {
  // Validate project prefix
  if (!/^[a-z][a-z0-9-]{1,20}$/.test(config.projectPrefix)) {
    throw new Error(
      'projectPrefix must start with a lowercase letter, contain only lowercase letters, numbers, and hyphens, and be 2-21 characters long.'
    );
  }

  // Validate AWS Region
  const validRegions = [
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'eu-west-1', 'eu-west-2', 'eu-central-1',
    'ap-northeast-1', 'ap-southeast-1', 'ap-southeast-2',
  ];
  if (!validRegions.includes(config.awsRegion)) {
    console.warn(`Warning: ${config.awsRegion} is not in the common regions list. Proceeding anyway.`);
  }

  // Validate VPC CIDR
  const cidrPattern = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/;
  if (!cidrPattern.test(config.vpcCidr)) {
    throw new Error(`Invalid VPC CIDR format: ${config.vpcCidr}`);
  }

  // Validate Gateway inbound auth selection. A typo here would otherwise
  // silently fall through to an unintended authorizer and 401 every Gateway
  // call, so fail fast at synth instead.
  if (!['jwt', 'iam'].includes(config.gateway.inboundAuth)) {
    throw new Error(
      `gateway.inboundAuth must be 'jwt' or 'iam'. Got: ${config.gateway.inboundAuth}`
    );
  }

  // Validate RAG Ingestion configuration (always provisioned).
  // Validate Lambda memory size (128 MB to 10240 MB)
  if (config.ragIngestion.lambdaMemorySize < 128 || config.ragIngestion.lambdaMemorySize > 10240) {
    throw new Error(
      `RAG Lambda memory size must be between 128 and 10240 MB. Got: ${config.ragIngestion.lambdaMemorySize}`
    );
  }

  // Validate Lambda timeout (1 to 900 seconds)
  if (config.ragIngestion.lambdaTimeout < 1 || config.ragIngestion.lambdaTimeout > 900) {
    throw new Error(
      `RAG Lambda timeout must be between 1 and 900 seconds. Got: ${config.ragIngestion.lambdaTimeout}`
    );
  }

  // Validate vector dimension (must be positive)
  if (config.ragIngestion.vectorDimension <= 0) {
    throw new Error(
      `RAG vector dimension must be positive. Got: ${config.ragIngestion.vectorDimension}`
    );
  }

  // Validate distance metric
  const validMetrics = ['cosine', 'euclidean', 'dot_product'];
  if (!validMetrics.includes(config.ragIngestion.vectorDistanceMetric)) {
    throw new Error(
      `RAG vector distance metric must be one of: ${validMetrics.join(', ')}. Got: ${config.ragIngestion.vectorDistanceMetric}`
    );
  }

  // Validate embedding model (basic check for non-empty string)
  if (!config.ragIngestion.embeddingModel || config.ragIngestion.embeddingModel.trim() === '') {
    throw new Error('RAG embedding model must be a non-empty string');
  }

  // Validate CORS origins if provided
  if (config.corsOrigins) {
    const origins = config.corsOrigins.split(',').map(o => o.trim());
    origins.forEach(origin => {
      if (origin && !origin.startsWith('http://') && !origin.startsWith('https://') && origin !== '*') {
        console.warn(`Warning: CORS origin '${origin}' should start with http:// or https:// or be '*'`);
      }
    });
  }

  // Validate top-level CORS origins.
  if (!config.corsOrigins) {
    console.warn(
      'Warning: no CORS origins configured. ' +
      'Set CDK_DOMAIN_NAME or CDK_CORS_ORIGINS to enable browser uploads.'
    );
  }

  // Validate required App API Fargate sizing (always provisioned).
  if (!config.appApi.cpu) {
    throw new Error('App API stack requires "cpu" to be set.');
  }
  if (!config.appApi.memory) {
    throw new Error('App API stack requires "memory" to be set.');
  }
  if (!config.appApi.desiredCount && config.appApi.desiredCount !== 0) {
    throw new Error('App API stack requires "desiredCount" to be set.');
  }
  if (!config.appApi.maxCapacity) {
    throw new Error('App API stack requires "maxCapacity" to be set.');
  }

  if (!config.frontend.cloudFrontPriceClass) {
    throw new Error('Frontend stack requires "cloudFrontPriceClass" to be set.');
  }

  // Artifacts and MCP Sandbox domain/cert validation is a deploy-time
  // concern — operators must set CDK_DOMAIN_NAME, CDK_HOSTED_ZONE_DOMAIN,
  // and the respective certificate ARNs for a real deployment. Synth and
  // tests proceed without them (constructs handle the undefined case by
  // falling back to CloudFront default domains).

  // ── Observability ──
  // CloudWatch Logs accepts only a fixed set of retention values; an arbitrary
  // number is rejected at deploy time, long after CI has gone green.
  const validRetentionDays = [
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096,
    1827, 2192, 2557, 2922, 3288, 3653,
  ];
  if (!validRetentionDays.includes(config.observability.logRetentionDays)) {
    throw new Error(
      `Invalid observability.logRetentionDays: ${config.observability.logRetentionDays}. ` +
      `CloudWatch Logs accepts only: ${validRetentionDays.join(', ')}. ` +
      `Set CDK_OBSERVABILITY_LOG_RETENTION_DAYS to one of those values.`
    );
  }

  // A rate, not a percentage: 5 instead of 0.05 is a 100x cost error.
  const rate = config.observability.xraySamplingRate;
  if (rate < 0 || rate > 1) {
    throw new Error(
      `Invalid observability.xraySamplingRate: ${rate}. ` +
      `Expected a rate between 0.0 and 1.0 (e.g. 0.05 for 5%), not a percentage. ` +
      `X-Ray bills per trace recorded, so a value above 1.0 is rejected rather ` +
      `than clamped.`
    );
  }

  const percentFields: Array<[string, number]> = [
    ['ecsCpuPercent', config.observability.ecsCpuPercent],
    ['ecsMemoryPercent', config.observability.ecsMemoryPercent],
    ['lambdaDurationPercentOfTimeout', config.observability.lambdaDurationPercentOfTimeout],
  ];
  for (const [name, value] of percentFields) {
    if (value <= 0 || value > 100) {
      throw new Error(
        `Invalid observability.${name}: ${value}. Expected a percentage between 1 and 100.`
      );
    }
  }
}

/**
 * Get the stack environment from configuration
 */
export function getStackEnv(config: AppConfig): cdk.Environment {
  return {
    account: config.awsAccount,
    region: config.awsRegion,
  };
}

/**
 * Generate a standardized resource name
 */
export function getResourceName(config: AppConfig, ...parts: string[]): string {
  const allParts = [config.projectPrefix, ...parts];
  return allParts.join('-');
}
/**
 * Generate a standardized resource name, truncated to a maximum length.
 * Truncates the prefix (left side) to fit within the limit while keeping
 * the suffix parts intact, since they carry the semantic meaning.
 *
 * @param maxLength Maximum allowed character length for the name
 */
export function getTruncatedResourceName(config: AppConfig, maxLength: number, ...parts: string[]): string {
  const fullName = getResourceName(config, ...parts);
  if (fullName.length <= maxLength) {
    return fullName;
  }
  // Keep suffix intact, truncate the prefix
  const suffix = parts.join('-');
  const available = maxLength - suffix.length - 1; // -1 for the joining hyphen
  if (available < 1) {
    // Suffix alone exceeds limit — just hard-truncate
    return fullName.slice(0, maxLength);
  }
  const truncatedPrefix = config.projectPrefix.slice(0, available);
  return `${truncatedPrefix}-${suffix}`;
}


/**
 * Get the removal policy based on retention configuration
 * @param config The application configuration
 * @returns RETAIN when retainDataOnDelete is true, DESTROY when false
 */
export function getRemovalPolicy(config: AppConfig): cdk.RemovalPolicy {
  return config.retainDataOnDelete 
    ? cdk.RemovalPolicy.RETAIN 
    : cdk.RemovalPolicy.DESTROY;
}

/**
 * Get the autoDeleteObjects setting for S3 buckets based on retention configuration
 * @param config The application configuration
 * @returns false when retainDataOnDelete is true, true when false
 */
export function getAutoDeleteObjects(config: AppConfig): boolean {
  return !config.retainDataOnDelete;
}

/**
 * Apply standard tags to a stack
 */
export function applyStandardTags(stack: cdk.Stack, config: AppConfig): void {
  // Inject Project tag dynamically from projectPrefix (can't interpolate in context)
  cdk.Tags.of(stack).add('Project', config.projectPrefix);
  // Add Version tag from appVersion (flows from VERSION file via CI/CD)
  cdk.Tags.of(stack).add('Version', config.appVersion);
  Object.entries(config.tags).forEach(([key, value]) => {
    cdk.Tags.of(stack).add(key, value);
  });
}

/**
 * Build the canonical CORS origins list for a stack.
 *
 * Always includes:
 *   1. https://{CDK_DOMAIN_NAME}  (from config.corsOrigins)
 *
 * Optionally appends extra origins from:
 *   - CDK_CORS_ORIGINS (already merged into config.corsOrigins)
 *   - additionalOrigins parameter (section-specific CDK_*_CORS_ORIGINS)
 *
 * localhost is NOT auto-included. Add it via CDK_CORS_ORIGINS for local dev.
 *
 * Returns a de-duplicated array suitable for S3 CORS rules or
 * a comma-joined string for container env vars.
 *
 * @param config  The top-level AppConfig
 * @param additionalOrigins  Optional comma-separated extra origins to append
 */
export function buildCorsOrigins(config: AppConfig, additionalOrigins?: string): string[] {
  const origins = new Set<string>();
  if (config.corsOrigins) {
    config.corsOrigins.split(',').map(o => o.trim()).filter(Boolean).forEach(o => origins.add(o));
  }
  if (additionalOrigins) {
    additionalOrigins.split(',').map(o => o.trim()).filter(Boolean).forEach(o => origins.add(o));
  }
  return Array.from(origins);
}
