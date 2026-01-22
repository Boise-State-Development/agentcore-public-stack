import * as cdk from 'aws-cdk-lib';

export interface AppConfig {
  environment: string; // 'prod', 'dev', 'test', etc.
  projectPrefix: string;
  awsAccount: string;
  awsRegion: string;
  vpcCidr: string;  
  infrastructureHostedZoneDomain?: string;
  albSubdomain?: string; // Subdomain for ALB (e.g., 'api' for api.yourdomain.com)
  certificateArn?: string; // ACM certificate ARN for HTTPS on ALB
  entraClientId: string; // Microsoft Entra (Azure AD) Client ID for OAuth
  entraTenantId: string; // Microsoft Entra (Azure AD) Tenant ID
  frontend: FrontendConfig;
  appApi: AppApiConfig;
  inferenceApi: InferenceApiConfig;
  gateway: GatewayConfig;
  assistants: AssistantsConfig;
  fileUpload: FileUploadConfig;
  tags: { [key: string]: string };
}

export interface FrontendConfig {
  domainName?: string;
  enableRoute53: boolean;
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
  databaseType: 'dynamodb' | 'rds' | 'none';
  enableRds: boolean;
  rdsInstanceClass?: string;
  rdsEngine?: string;
  rdsDatabaseName?: string;
  imageTag: string;
  entraRedirectUri: string;
}

export interface InferenceApiConfig {
  enabled: boolean;
  cpu: number;
  memory: number;
  desiredCount: number;
  maxCapacity: number;
  enableGpu: boolean;
  imageTag: string;
  // Environment variables for runtime container
  enableAuthentication: string;
  logLevel: string;
  uploadDir: string;
  outputDir: string;
  generatedImagesDir: string;
  apiUrl: string;
  frontendUrl: string;
  corsOrigins: string;
  tavilyApiKey: string;
  novaActApiKey: string;
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

/**
 * Load and validate configuration from CDK context
 * @param scope The CDK construct scope
 * @returns Validated AppConfig object
 */
export function loadConfig(scope: cdk.App): AppConfig {
  // Load configuration from context
  const projectPrefix = getRequiredContext(scope, 'projectPrefix');
  const awsRegion = getRequiredContext(scope, 'awsRegion');
  
  // AWS Account can come from context or environment variable
  const awsAccount = scope.node.tryGetContext('awsAccount') || 
                     process.env.CDK_DEFAULT_ACCOUNT ||
                     process.env.AWS_ACCOUNT_ID;
  
  if (!awsAccount) {
    throw new Error(
      'AWS Account ID is required. Set it in cdk.context.json or via CDK_DEFAULT_ACCOUNT/AWS_ACCOUNT_ID environment variable.'
    );
  }

  const environment = scope.node.tryGetContext('environment') || process.env.DEPLOY_ENVIRONMENT || 'prod';

  const config: AppConfig = {
    environment,
    projectPrefix,
    awsAccount,
    awsRegion,
    vpcCidr: scope.node.tryGetContext('vpcCidr'),    
    infrastructureHostedZoneDomain: process.env.CDK_HOSTED_ZONE_DOMAIN || scope.node.tryGetContext('infrastructureHostedZoneDomain'),
    albSubdomain: process.env.CDK_ALB_SUBDOMAIN || scope.node.tryGetContext('albSubdomain'),
    certificateArn: process.env.CDK_CERTIFICATE_ARN || scope.node.tryGetContext('certificateArn'),
    entraClientId: process.env.CDK_ENTRA_CLIENT_ID || scope.node.tryGetContext('entraClientId'),
    entraTenantId: process.env.CDK_ENTRA_TENANT_ID || scope.node.tryGetContext('entraTenantId'),
    frontend: {
      domainName: process.env.CDK_FRONTEND_DOMAIN_NAME || scope.node.tryGetContext('frontend').domainName,
      enableRoute53: parseBooleanEnv(process.env.CDK_FRONTEND_ENABLE_ROUTE53) ?? scope.node.tryGetContext('frontend').enableRoute53,
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
      databaseType: 'none', // Set to 'dynamodb' or 'rds' when database is needed
      enableRds: false,
      entraRedirectUri: process.env.CDK_APP_API_ENTRA_REDIRECT_URI || scope.node.tryGetContext('appApi')?.entraRedirectUri,
    },
    inferenceApi: {
      enabled: parseBooleanEnv(process.env.CDK_INFERENCE_API_ENABLED) ?? scope.node.tryGetContext('inferenceApi')?.enabled,
      cpu: parseIntEnv(process.env.CDK_INFERENCE_API_CPU) || scope.node.tryGetContext('inferenceApi')?.cpu,
      memory: parseIntEnv(process.env.CDK_INFERENCE_API_MEMORY) || scope.node.tryGetContext('inferenceApi')?.memory,
      desiredCount: parseIntEnv(process.env.CDK_INFERENCE_API_DESIRED_COUNT) ?? scope.node.tryGetContext('inferenceApi')?.desiredCount,
      maxCapacity: parseIntEnv(process.env.CDK_INFERENCE_API_MAX_CAPACITY) || scope.node.tryGetContext('inferenceApi')?.maxCapacity,
      enableGpu: parseBooleanEnv(process.env.CDK_INFERENCE_API_ENABLE_GPU) ?? scope.node.tryGetContext('inferenceApi')?.enableGpu,
      imageTag: scope.node.tryGetContext('imageTag') || '',
      // Environment variables from GitHub Secrets/Variables with context fallback
      enableAuthentication: process.env.ENV_INFERENCE_API_ENABLE_AUTHENTICATION || scope.node.tryGetContext('inferenceApi')?.enableAuthentication,
      logLevel: process.env.ENV_INFERENCE_API_LOG_LEVEL || scope.node.tryGetContext('inferenceApi')?.logLevel,
      uploadDir: process.env.ENV_INFERENCE_API_UPLOAD_DIR || scope.node.tryGetContext('inferenceApi')?.uploadDir,
      outputDir: process.env.ENV_INFERENCE_API_OUTPUT_DIR || scope.node.tryGetContext('inferenceApi')?.outputDir,
      generatedImagesDir: process.env.ENV_INFERENCE_API_GENERATED_IMAGES_DIR || scope.node.tryGetContext('inferenceApi')?.generatedImagesDir,
      apiUrl: process.env.ENV_INFERENCE_API_API_URL || scope.node.tryGetContext('inferenceApi')?.apiUrl,
      frontendUrl: process.env.ENV_INFERENCE_API_FRONTEND_URL || scope.node.tryGetContext('inferenceApi')?.frontendUrl,
      corsOrigins: process.env.ENV_INFERENCE_API_CORS_ORIGINS || scope.node.tryGetContext('inferenceApi')?.corsOrigins,
      tavilyApiKey: process.env.ENV_INFERENCE_API_TAVILY_API_KEY || scope.node.tryGetContext('inferenceApi')?.tavilyApiKey,
      novaActApiKey: process.env.ENV_INFERENCE_API_NOVA_ACT_API_KEY || scope.node.tryGetContext('inferenceApi')?.novaActApiKey,
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
      enabled: parseBooleanEnv(process.env.CDK_FILE_UPLOAD_ENABLED) ?? scope.node.tryGetContext('fileUpload')?.enabled ?? true,
      maxFileSizeBytes: parseIntEnv(process.env.CDK_FILE_UPLOAD_MAX_FILE_SIZE) || scope.node.tryGetContext('fileUpload')?.maxFileSizeBytes || 4 * 1024 * 1024, // 4MB
      maxFilesPerMessage: parseIntEnv(process.env.CDK_FILE_UPLOAD_MAX_FILES_PER_MESSAGE) || scope.node.tryGetContext('fileUpload')?.maxFilesPerMessage || 5,
      userQuotaBytes: parseIntEnv(process.env.CDK_FILE_UPLOAD_USER_QUOTA) || scope.node.tryGetContext('fileUpload')?.userQuotaBytes || 1024 * 1024 * 1024, // 1GB
      retentionDays: parseIntEnv(process.env.CDK_FILE_UPLOAD_RETENTION_DAYS) || scope.node.tryGetContext('fileUpload')?.retentionDays || 365,
      corsOrigins: process.env.CDK_FILE_UPLOAD_CORS_ORIGINS || scope.node.tryGetContext('fileUpload')?.corsOrigins,
    },
    assistants: {
      enabled: parseBooleanEnv(process.env.CDK_ASSISTANTS_ENABLED) ?? scope.node.tryGetContext('assistants')?.enabled ?? true,
      corsOrigins: process.env.CDK_ASSISTANTS_CORS_ORIGINS || scope.node.tryGetContext('assistants')?.corsOrigins,
    },
    tags: {
      Environment: environment,
      Project: projectPrefix,
      ManagedBy: 'CDK',
      ...scope.node.tryGetContext('tags'),
    },
  };

  // Validate configuration
  validateConfig(config);

  return config;
}

/**
 * Get required context value or throw error
 */
function getRequiredContext(scope: cdk.App, key: string): string {
  const value = scope.node.tryGetContext(key);
  if (!value) {
    throw new Error(
      `Required context value '${key}' is missing. Please set it in cdk.context.json.`
    );
  }
  return value;
}

/**
 * Parse boolean environment variable
 * Returns undefined if the value is not set, allowing for fallback logic
 */
function parseBooleanEnv(value: string | undefined): boolean | undefined {
  if (value === undefined || value === '') {
    return undefined;
  }
  return value.toLowerCase() === 'true';
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

  // // Validate Route53 domain if enabled
  // if (config.enableRoute53 && !config.domainName) {
  //   throw new Error('domainName is required when enableRoute53 is true.');
  // }

  // // Validate certificate ARN if domain is configured
  // if (config.domainName && !config.certificateArn) {
  //   console.warn('Warning: domainName is set but certificateArn is not provided. HTTPS will not be configured.');
  // }
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
 * Generate a standardized resource name with environment suffix for non-prod
 */
export function getResourceName(config: AppConfig, ...parts: string[]): string {
  // Add environment suffix for non-prod environments
  const envSuffix = config.environment === 'prod' ? '' : `-${config.environment}`;
  const allParts = [config.projectPrefix + envSuffix, ...parts];
  return allParts.join('-');
}

/**
 * Apply standard tags to a stack
 */
export function applyStandardTags(stack: cdk.Stack, config: AppConfig): void {
  Object.entries(config.tags).forEach(([key, value]) => {
    cdk.Tags.of(stack).add(key, value);
  });
}
