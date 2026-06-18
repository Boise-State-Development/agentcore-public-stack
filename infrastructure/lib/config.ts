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
  fineTuning: FineTuningConfig;
  artifacts: ArtifactsConfig;
  mcpSandbox: McpSandboxConfig;
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

export interface FineTuningConfig {
  additionalCorsOrigins?: string; // Extra CORS origins to append (comma-separated)
}

/**
 * Load and validate configuration from CDK context
 * @param scope The CDK construct scope
 * @returns Validated AppConfig object
 */
export function loadConfig(scope: cdk.App): AppConfig {
  // Load required configuration from environment variables or context
  const projectPrefix = process.env.CDK_PROJECT_PREFIX || scope.node.tryGetContext('projectPrefix');
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
    fineTuning: {
      additionalCorsOrigins: process.env.CDK_FINE_TUNING_CORS_ORIGINS || scope.node.tryGetContext('fineTuning')?.additionalCorsOrigins,
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
    tags: {
      ...(scope.node.tryGetContext('tags') || {}),
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
  console.log(`   App Version: ${config.appVersion}`);

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
