import * as cdk from 'aws-cdk-lib';

export interface AppConfig {
  environment: string; // 'prod', 'dev', 'test', etc.
  projectPrefix: string;
  awsAccount: string;
  awsRegion: string;
  vpcCidr: string;  
  infrastructureHostedZoneDomain?: string;
  frontend: FrontendConfig;
  appApi: AppApiConfig;
  inferenceApi: InferenceApiConfig;
  gateway: GatewayConfig;
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
    vpcCidr: scope.node.tryGetContext('vpcCidr') || '10.0.0.0/16',    
    infrastructureHostedZoneDomain: process.env.CDK_HOSTED_ZONE_DOMAIN || scope.node.tryGetContext('infrastructureHostedZoneDomain'),
    frontend: {
      domainName: process.env.CDK_FRONTEND_DOMAIN_NAME || scope.node.tryGetContext('frontend').domainName,
      enableRoute53: parseBooleanEnv(process.env.CDK_FRONTEND_ENABLE_ROUTE53) ?? scope.node.tryGetContext('frontend').enableRoute53 ?? false,
      certificateArn: process.env.CDK_FRONTEND_CERTIFICATE_ARN || scope.node.tryGetContext('frontend').certificateArn,
      enabled: parseBooleanEnv(process.env.CDK_FRONTEND_ENABLED) ?? scope.node.tryGetContext('frontend')?.enabled ?? true,
      bucketName: process.env.CDK_FRONTEND_BUCKET_NAME || scope.node.tryGetContext('frontend')?.bucketName,
      cloudFrontPriceClass: process.env.CDK_FRONTEND_CLOUDFRONT_PRICE_CLASS || scope.node.tryGetContext('frontend')?.cloudFrontPriceClass || 'PriceClass_100',
    },
    appApi: {
      enabled: parseBooleanEnv(process.env.CDK_APP_API_ENABLED) ?? scope.node.tryGetContext('appApi')?.enabled ?? true,
      cpu: parseIntEnv(process.env.CDK_APP_API_CPU) || scope.node.tryGetContext('appApi')?.cpu || 512,
      memory: parseIntEnv(process.env.CDK_APP_API_MEMORY) || scope.node.tryGetContext('appApi')?.memory || 1024,
      desiredCount: parseIntEnv(process.env.CDK_APP_API_DESIRED_COUNT) ?? scope.node.tryGetContext('appApi')?.desiredCount ?? 0,
      imageTag: scope.node.tryGetContext('imageTag') || '',
      maxCapacity: parseIntEnv(process.env.CDK_APP_API_MAX_CAPACITY) || scope.node.tryGetContext('appApi')?.maxCapacity || 10,
      databaseType: 'none', // Set to 'dynamodb' or 'rds' when database is needed
      enableRds: false,
    },
    inferenceApi: {
      enabled: true,
      cpu: 1024,
      memory: 2048,
      desiredCount: 1,
      maxCapacity: 5,
      enableGpu: false,
      imageTag: scope.node.tryGetContext('imageTag') || '',
      // Environment variables from GitHub Secrets/Variables with context fallback
      enableAuthentication: process.env.ENABLE_AUTHENTICATION || scope.node.tryGetContext('inferenceApi')?.enableAuthentication || 'true',
      logLevel: process.env.LOG_LEVEL || scope.node.tryGetContext('inferenceApi')?.logLevel || 'INFO',
      uploadDir: process.env.UPLOAD_DIR || scope.node.tryGetContext('inferenceApi')?.uploadDir || 'uploads',
      outputDir: process.env.OUTPUT_DIR || scope.node.tryGetContext('inferenceApi')?.outputDir || 'output',
      generatedImagesDir: process.env.GENERATED_IMAGES_DIR || scope.node.tryGetContext('inferenceApi')?.generatedImagesDir || 'generated_images',
      apiUrl: process.env.API_URL || scope.node.tryGetContext('inferenceApi')?.apiUrl || '',
      frontendUrl: process.env.FRONTEND_URL || scope.node.tryGetContext('inferenceApi')?.frontendUrl || '',
      corsOrigins: process.env.CORS_ORIGINS || scope.node.tryGetContext('inferenceApi')?.corsOrigins || '',
      tavilyApiKey: process.env.TAVILY_API_KEY || scope.node.tryGetContext('inferenceApi')?.tavilyApiKey || '',
      novaActApiKey: process.env.NOVA_ACT_API_KEY || scope.node.tryGetContext('inferenceApi')?.novaActApiKey || '',
    },
    gateway: {
      enabled: parseBooleanEnv(process.env.CDK_GATEWAY_ENABLED) ?? scope.node.tryGetContext('gateway')?.enabled ?? true,
      apiType: (process.env.CDK_GATEWAY_API_TYPE as 'REST' | 'HTTP') || scope.node.tryGetContext('gateway')?.apiType || 'HTTP',
      throttleRateLimit: parseIntEnv(process.env.CDK_GATEWAY_THROTTLE_RATE_LIMIT) || scope.node.tryGetContext('gateway')?.throttleRateLimit || 10000,
      throttleBurstLimit: parseIntEnv(process.env.CDK_GATEWAY_THROTTLE_BURST_LIMIT) || scope.node.tryGetContext('gateway')?.throttleBurstLimit || 5000,
      enableWaf: parseBooleanEnv(process.env.CDK_GATEWAY_ENABLE_WAF) ?? scope.node.tryGetContext('gateway')?.enableWaf ?? false,
      logLevel: process.env.CDK_GATEWAY_LOG_LEVEL || scope.node.tryGetContext('gateway')?.logLevel || 'INFO',
    },
    tags: {
      Environment: environment,
      Project: projectPrefix,
      ManagedBy: 'CDK',
      ...scope.node.tryGetContext('tags'),
    },
  };

  // console.log the loaded config for debugging
  console.log('Loaded AppConfig:', JSON.stringify(config, null, 2));

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
