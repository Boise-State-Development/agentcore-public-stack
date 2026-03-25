import * as cdk from 'aws-cdk-lib';

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
  certificateArn?: string; // ACM certificate ARN for HTTPS on ALB
  frontend: FrontendConfig;
  appApi: AppApiConfig;
  inferenceApi: InferenceApiConfig;
  gateway: GatewayConfig;
  assistants: AssistantsConfig;
  fileUpload: FileUploadConfig;
  ragIngestion: RagIngestionConfig;
  fineTuning: FineTuningConfig;
  appVersion: string;
  tags: { [key: string]: string };
}

export interface FrontendConfig {
  certificateArn?: string;
  enabled: boolean;
  bucketName?: string;
  cloudFrontPriceClass: string;
}

export interface AssistantsConfig {
  enabled: boolean;
  corsOrigins: string;
}

export interface AppApiConfig {
  enabled: boolean;
  cpu: number;
  memory: number;
  desiredCount: number;
  maxCapacity: number;
  imageTag: string;
}

export interface InferenceApiConfig {
  enabled: boolean;
  cpu: number;
  memory: number;
  desiredCount: number;
  maxCapacity: number;
  imageTag: string;
  // Environment variables for runtime container
  logLevel: string;
  corsOrigins: string;
}

export interface GatewayConfig {
  enabled: boolean;
  apiType: 'REST' | 'HTTP';
  throttleRateLimit: number;
  throttleBurstLimit: number;
  enableWaf: boolean;
  logLevel?: string;  // Log level for Lambda functions (INFO, DEBUG, ERROR)
}

export interface FileUploadConfig {
  enabled: boolean;
  maxFileSizeBytes: number;      // Maximum file size (default: 4MB per Bedrock limit)
  maxFilesPerMessage: number;    // Maximum files per message (default: 5)
  userQuotaBytes: number;        // Per-user storage quota (default: 1GB)
  retentionDays: number;         // File retention (default: 365 days)
  corsOrigins?: string;          // Comma-separated CORS origins (defaults based on environment)
}

export interface RagIngestionConfig {
  enabled: boolean;              // Enable/disable RAG stack
  corsOrigins: string;           // Comma-separated CORS origins
  lambdaMemorySize: number;      // Lambda memory in MB (default: 3008)
  lambdaTimeout: number;         // Lambda timeout in seconds (default: 900)
  embeddingModel: string;        // Bedrock model ID (default: "amazon.titan-embed-text-v2")
  vectorDimension: number;       // Embedding dimension (default: 1024)
  vectorDistanceMetric: string;  // Distance metric (default: "cosine")
}

export interface FineTuningConfig {
  enabled: boolean;              // Enable/disable SageMaker Fine-Tuning stack
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

  // Top-level shared CORS origins — used as default for sections that don't override.
  // If not explicitly set, auto-derive from CDK_DOMAIN_NAME so callers only need one variable.
  const domainName = process.env.CDK_DOMAIN_NAME || scope.node.tryGetContext('domainName');
  const corsOrigins = process.env.CDK_CORS_ORIGINS
    || scope.node.tryGetContext('corsOrigins')
    || (domainName ? `https://${domainName}` : '');

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
    frontend: {
      certificateArn: process.env.CDK_FRONTEND_CERTIFICATE_ARN || scope.node.tryGetContext('frontend').certificateArn,
      enabled: parseBooleanEnv(process.env.CDK_FRONTEND_ENABLED) ?? scope.node.tryGetContext('frontend')?.enabled,
      bucketName: process.env.CDK_FRONTEND_BUCKET_NAME || scope.node.tryGetContext('frontend')?.bucketName,
      cloudFrontPriceClass: process.env.CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS || scope.node.tryGetContext('frontend')?.cloudFrontPriceClass,
    },
    appApi: {
      enabled: parseBooleanEnv(process.env.CDK_APP_API_ENABLED) ?? scope.node.tryGetContext('appApi')?.enabled,
      cpu: parseIntEnv(process.env.CDK_APP_API_CPU) || scope.node.tryGetContext('appApi')?.cpu,
      memory: parseIntEnv(process.env.CDK_APP_API_MEMORY) || scope.node.tryGetContext('appApi')?.memory,
      desiredCount: parseIntEnv(process.env.CDK_APP_API_DESIRED_COUNT) ?? scope.node.tryGetContext('appApi')?.desiredCount,
      imageTag: scope.node.tryGetContext('imageTag') || '',
      maxCapacity: parseIntEnv(process.env.CDK_APP_API_MAX_CAPACITY) || scope.node.tryGetContext('appApi')?.maxCapacity,
    },
    inferenceApi: {
      enabled: parseBooleanEnv(process.env.CDK_INFERENCE_API_ENABLED) ?? scope.node.tryGetContext('inferenceApi')?.enabled,
      cpu: parseIntEnv(process.env.CDK_INFERENCE_API_CPU) || scope.node.tryGetContext('inferenceApi')?.cpu,
      memory: parseIntEnv(process.env.CDK_INFERENCE_API_MEMORY) || scope.node.tryGetContext('inferenceApi')?.memory,
      desiredCount: parseIntEnv(process.env.CDK_INFERENCE_API_DESIRED_COUNT) ?? scope.node.tryGetContext('inferenceApi')?.desiredCount,
      maxCapacity: parseIntEnv(process.env.CDK_INFERENCE_API_MAX_CAPACITY) || scope.node.tryGetContext('inferenceApi')?.maxCapacity,
      imageTag: scope.node.tryGetContext('imageTag') || '',
      // Environment variables from GitHub Secrets/Variables with context fallback
      logLevel: process.env.ENV_INFERENCE_API_LOG_LEVEL || scope.node.tryGetContext('inferenceApi')?.logLevel,
      corsOrigins: process.env.ENV_INFERENCE_API_CORS_ORIGINS || scope.node.tryGetContext('inferenceApi')?.corsOrigins,
    },
    gateway: {
      enabled: parseBooleanEnv(process.env.CDK_GATEWAY_ENABLED) ?? scope.node.tryGetContext('gateway')?.enabled,
      apiType: (process.env.CDK_GATEWAY_API_TYPE as 'REST' | 'HTTP') || scope.node.tryGetContext('gateway')?.apiType,
      throttleRateLimit: parseIntEnv(process.env.CDK_GATEWAY_THROTTLE_RATE_LIMIT) || scope.node.tryGetContext('gateway')?.throttleRateLimit,
      throttleBurstLimit: parseIntEnv(process.env.CDK_GATEWAY_THROTTLE_BURST_LIMIT) || scope.node.tryGetContext('gateway')?.throttleBurstLimit,
      enableWaf: parseBooleanEnv(process.env.CDK_GATEWAY_ENABLE_WAF) ?? scope.node.tryGetContext('gateway')?.enableWaf,
      logLevel: process.env.CDK_GATEWAY_LOG_LEVEL || scope.node.tryGetContext('gateway')?.logLevel,
    },
    fileUpload: {
      enabled: parseBooleanEnv(process.env.CDK_FILE_UPLOAD_ENABLED) ?? scope.node.tryGetContext('fileUpload')?.enabled,
      maxFileSizeBytes: parseIntEnv(process.env.CDK_FILE_UPLOAD_MAX_FILE_SIZE) || scope.node.tryGetContext('fileUpload')?.maxFileSizeBytes,
      maxFilesPerMessage: parseIntEnv(process.env.CDK_FILE_UPLOAD_MAX_FILES_PER_MESSAGE) || scope.node.tryGetContext('fileUpload')?.maxFilesPerMessage,
      userQuotaBytes: parseIntEnv(process.env.CDK_FILE_UPLOAD_USER_QUOTA) || scope.node.tryGetContext('fileUpload')?.userQuotaBytes,
      retentionDays: parseIntEnv(process.env.CDK_FILE_UPLOAD_RETENTION_DAYS) || scope.node.tryGetContext('fileUpload')?.retentionDays,
      corsOrigins: process.env.CDK_FILE_UPLOAD_CORS_ORIGINS || scope.node.tryGetContext('fileUpload')?.corsOrigins || corsOrigins,
    },
    assistants: {
      enabled: parseBooleanEnv(process.env.CDK_ASSISTANTS_ENABLED) ?? scope.node.tryGetContext('assistants')?.enabled,
      corsOrigins: process.env.CDK_ASSISTANTS_CORS_ORIGINS || scope.node.tryGetContext('assistants')?.corsOrigins || corsOrigins,
    },
    ragIngestion: {
      enabled: parseBooleanEnv(process.env.CDK_RAG_ENABLED) ?? scope.node.tryGetContext('ragIngestion')?.enabled,
      corsOrigins: process.env.CDK_RAG_CORS_ORIGINS || scope.node.tryGetContext('ragIngestion')?.corsOrigins || corsOrigins,
      lambdaMemorySize: parseIntEnv(process.env.CDK_RAG_LAMBDA_MEMORY) || scope.node.tryGetContext('ragIngestion')?.lambdaMemorySize,
      lambdaTimeout: parseIntEnv(process.env.CDK_RAG_LAMBDA_TIMEOUT) || scope.node.tryGetContext('ragIngestion')?.lambdaTimeout,
      embeddingModel: process.env.CDK_RAG_EMBEDDING_MODEL || scope.node.tryGetContext('ragIngestion')?.embeddingModel,
      vectorDimension: parseIntEnv(process.env.CDK_RAG_VECTOR_DIMENSION) || scope.node.tryGetContext('ragIngestion')?.vectorDimension,
      vectorDistanceMetric: process.env.CDK_RAG_DISTANCE_METRIC || scope.node.tryGetContext('ragIngestion')?.vectorDistanceMetric,
    },
    fineTuning: {
      enabled: parseBooleanEnv(process.env.CDK_FINE_TUNING_ENABLED) ?? scope.node.tryGetContext('fineTuning')?.enabled ?? false,
    },
    tags: {
      ...(scope.node.tryGetContext('tags') || {}),
    },
  };

  // Log loaded configuration for debugging
  console.log('📋 Loaded CDK Configuration:');
  console.log(`   Project Prefix: ${config.projectPrefix}`);
  console.log(`   AWS Region: ${config.awsRegion}`);
  console.log(`   Production: ${config.production}`);
  console.log(`   Retain Data on Delete: ${config.retainDataOnDelete}`);
  console.log(`   Frontend Enabled: ${config.frontend.enabled}`);
  console.log(`   App API Enabled: ${config.appApi.enabled}`);
  console.log(`   Inference API Enabled: ${config.inferenceApi.enabled}`);
  console.log(`   Gateway Enabled: ${config.gateway.enabled}`);
  console.log(`   Fine-Tuning Enabled: ${config.fineTuning.enabled}`);
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

  // Validate RAG Ingestion configuration
  if (config.ragIngestion.enabled) {
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
    if (config.ragIngestion.corsOrigins) {
      const origins = config.ragIngestion.corsOrigins.split(',').map(o => o.trim());
      origins.forEach(origin => {
        if (origin && !origin.startsWith('http://') && !origin.startsWith('https://') && origin !== '*') {
          console.warn(`Warning: RAG CORS origin '${origin}' should start with http:// or https:// or be '*'`);
        }
      });
    }
  }

  // Validate Gateway configuration
  if (config.gateway.enabled) {
    const validApiTypes = ['REST', 'HTTP'];
    if (!config.gateway.apiType || !validApiTypes.includes(config.gateway.apiType)) {
      throw new Error(
        `Gateway stack requires apiType to be 'REST' or 'HTTP'. Got: '${config.gateway.apiType}'`
      );
    }
  }

  // Validate File Upload CORS origins
  if (config.fileUpload.enabled) {
    const effectiveCors = config.fileUpload.corsOrigins || config.corsOrigins || config.domainName;
    if (!effectiveCors || effectiveCors.trim() === '') {
      throw new Error(
        'File Upload stack requires CORS origins to be configured. ' +
        'Set CDK_DOMAIN_NAME, CDK_CORS_ORIGINS, or corsOrigins in the fileUpload section.'
      );
    }
  }

  // Validate required fields for all enabled stacks
  if (config.appApi.enabled) {
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
  }

  if (config.inferenceApi.enabled) {
    if (!config.inferenceApi.cpu) {
      throw new Error('Inference API stack requires "cpu" to be set.');
    }
    if (!config.inferenceApi.memory) {
      throw new Error('Inference API stack requires "memory" to be set.');
    }
    if (!config.inferenceApi.desiredCount && config.inferenceApi.desiredCount !== 0) {
      throw new Error('Inference API stack requires "desiredCount" to be set.');
    }
    if (!config.inferenceApi.maxCapacity) {
      throw new Error('Inference API stack requires "maxCapacity" to be set.');
    }
  }

  if (config.frontend.enabled) {
    if (!config.frontend.cloudFrontPriceClass) {
      throw new Error('Frontend stack requires "cloudFrontPriceClass" to be set.');
    }
  }

  if (config.gateway.enabled) {
    if (!config.gateway.throttleRateLimit) {
      throw new Error('Gateway stack requires "throttleRateLimit" to be set.');
    }
    if (!config.gateway.throttleBurstLimit) {
      throw new Error('Gateway stack requires "throttleBurstLimit" to be set.');
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
