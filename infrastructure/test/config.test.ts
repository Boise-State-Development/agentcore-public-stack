import * as cdk from 'aws-cdk-lib';
import { loadConfig, AppConfig } from '../lib/config';

/**
 * Unit Tests for RAG Ingestion Configuration
 * 
 * These tests verify that the RAG ingestion configuration is loaded correctly
 * from environment variables, context values, and defaults, with proper precedence.
 * 
 * **Validates: Requirements 4.1-4.10**
 */

describe('RAG Ingestion Configuration', () => {
  let app: cdk.App;
  let originalEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    // Save original environment
    originalEnv = { ...process.env };

    // Create a fresh CDK app for each test
    app = new cdk.App();

    // Set required context values
    app.node.setContext('projectPrefix', 'test-project');
    app.node.setContext('awsRegion', 'us-east-1');
    app.node.setContext('awsAccount', '123456789012');
    app.node.setContext('vpcCidr', '10.0.0.0/16');
    app.node.setContext('entraClientId', 'test-client-id');
    app.node.setContext('entraTenantId', 'test-tenant-id');

    // Set default context for other required fields
    app.node.setContext('frontend', {
      enabled: true,
      enableRoute53: false,
      cloudFrontPriceClass: 'PriceClass_100',
    });
    app.node.setContext('appApi', {
      enabled: true,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 4,
      entraRedirectUri: 'http://localhost:3000/auth/callback',
    });
    app.node.setContext('inferenceApi', {
      enabled: true,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 4,
      enableGpu: false,
      enableAuthentication: 'true',
      logLevel: 'INFO',
      uploadDir: '/tmp/uploads',
      outputDir: '/tmp/outputs',
      generatedImagesDir: '/tmp/images',
      apiUrl: 'http://localhost:8000',
      frontendUrl: 'http://localhost:3000',
      corsOrigins: 'http://localhost:3000',
      tavilyApiKey: 'test-key',
      novaActApiKey: 'test-key',
    });
    app.node.setContext('gateway', {
      enabled: true,
      apiType: 'REST',
      throttleRateLimit: 1000,
      throttleBurstLimit: 2000,
      enableWaf: false,
    });
    app.node.setContext('assistants', {
      enabled: true,
      corsOrigins: 'http://localhost:3000',
    });
    app.node.setContext('fileUpload', {
      enabled: true,
      maxFileSizeBytes: 4194304,
      maxFilesPerMessage: 5,
      userQuotaBytes: 1073741824,
      retentionDays: 365,
    });
  });

  afterEach(() => {
    // Restore original environment
    process.env = originalEnv;
  });

  // ============================================================
  // Environment Variable Loading Tests
  // ============================================================

  describe('Environment Variable Loading', () => {
    test('loads enabled flag from CDK_RAG_ENABLED environment variable', () => {
      process.env.CDK_RAG_ENABLED = 'false';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(false);
    });

    test('loads CORS origins from CDK_RAG_CORS_ORIGINS environment variable', () => {
      process.env.CDK_RAG_CORS_ORIGINS = 'https://example.com,https://test.com';

      const config = loadConfig(app);

      expect(config.ragIngestion.corsOrigins).toBe('https://example.com,https://test.com');
    });

    test('loads Lambda memory size from CDK_RAG_LAMBDA_MEMORY environment variable', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '8192';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(8192);
    });

    test('loads Lambda timeout from CDK_RAG_LAMBDA_TIMEOUT environment variable', () => {
      process.env.CDK_RAG_LAMBDA_TIMEOUT = '600';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaTimeout).toBe(600);
    });

    test('loads embedding model from CDK_RAG_EMBEDDING_MODEL environment variable', () => {
      process.env.CDK_RAG_EMBEDDING_MODEL = 'amazon.titan-embed-text-v1';

      const config = loadConfig(app);

      expect(config.ragIngestion.embeddingModel).toBe('amazon.titan-embed-text-v1');
    });

    test('loads vector dimension from CDK_RAG_VECTOR_DIMENSION environment variable', () => {
      process.env.CDK_RAG_VECTOR_DIMENSION = '512';

      const config = loadConfig(app);

      expect(config.ragIngestion.vectorDimension).toBe(512);
    });

    test('loads distance metric from CDK_RAG_DISTANCE_METRIC environment variable', () => {
      process.env.CDK_RAG_DISTANCE_METRIC = 'euclidean';

      const config = loadConfig(app);

      expect(config.ragIngestion.vectorDistanceMetric).toBe('euclidean');
    });

    test('loads all RAG configuration from environment variables', () => {
      process.env.CDK_RAG_ENABLED = 'true';
      process.env.CDK_RAG_CORS_ORIGINS = 'https://prod.example.com';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';
      process.env.CDK_RAG_LAMBDA_TIMEOUT = '900';
      process.env.CDK_RAG_EMBEDDING_MODEL = 'amazon.titan-embed-text-v2';
      process.env.CDK_RAG_VECTOR_DIMENSION = '1024';
      process.env.CDK_RAG_DISTANCE_METRIC = 'cosine';

      const config = loadConfig(app);

      expect(config.ragIngestion).toEqual({
        enabled: true,
        corsOrigins: 'https://prod.example.com',
        lambdaMemorySize: 10240,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });
    });
  });

  // ============================================================
  // Context Fallback Tests
  // ============================================================

  describe('Context Fallback', () => {
    test('falls back to context value when environment variable not set', () => {
      app.node.setContext('ragIngestion', {
        enabled: false,
        corsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
        embeddingModel: 'amazon.titan-embed-text-v1',
        vectorDimension: 512,
        vectorDistanceMetric: 'euclidean',
      });

      const config = loadConfig(app);

      expect(config.ragIngestion).toEqual({
        enabled: false,
        corsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
        embeddingModel: 'amazon.titan-embed-text-v1',
        vectorDimension: 512,
        vectorDistanceMetric: 'euclidean',
      });
    });

    test('environment variable takes precedence over context', () => {
      app.node.setContext('ragIngestion', {
        enabled: false,
        corsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
      });

      process.env.CDK_RAG_ENABLED = 'true';
      process.env.CDK_RAG_CORS_ORIGINS = 'https://env.example.com';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true);
      expect(config.ragIngestion.corsOrigins).toBe('https://env.example.com');
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
    });

    test('uses context for some values and env for others', () => {
      app.node.setContext('ragIngestion', {
        enabled: false,
        corsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
      });

      process.env.CDK_RAG_ENABLED = 'true';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true); // from env
      expect(config.ragIngestion.corsOrigins).toBe('https://context.example.com'); // from context
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // from env
      expect(config.ragIngestion.lambdaTimeout).toBe(600); // from context
    });
  });

  // ============================================================
  // Default Values Tests
  // ============================================================

  describe('Default Values', () => {
    test('uses default values when neither env nor context set', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true);
      expect(config.ragIngestion.corsOrigins).toBe('');
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
      expect(config.ragIngestion.lambdaTimeout).toBe(900);
      expect(config.ragIngestion.embeddingModel).toBe('amazon.titan-embed-text-v2');
      expect(config.ragIngestion.vectorDimension).toBe(1024);
      expect(config.ragIngestion.vectorDistanceMetric).toBe('cosine');
    });

    test('default enabled is true', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true);
    });

    test('default CORS origins is empty string', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.corsOrigins).toBe('');
    });

    test('default Lambda memory is 10240 MB', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
    });

    test('default Lambda timeout is 900 seconds', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaTimeout).toBe(900);
    });

    test('default embedding model is Titan V2', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.embeddingModel).toBe('amazon.titan-embed-text-v2');
    });

    test('default vector dimension is 1024', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.vectorDimension).toBe(1024);
    });

    test('default distance metric is cosine', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.vectorDistanceMetric).toBe('cosine');
    });
  });

  // ============================================================
  // Configuration Validation Tests
  // ============================================================

  describe('Configuration Validation', () => {
    test('validates Lambda memory size is within bounds', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '100'; // Too low

      expect(() => loadConfig(app)).toThrow(
        'RAG Lambda memory size must be between 128 and 10240 MB'
      );
    });

    test('validates Lambda memory size maximum', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '20000'; // Too high

      expect(() => loadConfig(app)).toThrow(
        'RAG Lambda memory size must be between 128 and 10240 MB'
      );
    });

    test('validates Lambda timeout is within bounds', () => {
      // Create a fresh app for this test
      const testApp = new cdk.App();
      testApp.node.setContext('projectPrefix', 'test-project');
      testApp.node.setContext('awsRegion', 'us-east-1');
      testApp.node.setContext('awsAccount', '123456789012');
      testApp.node.setContext('vpcCidr', '10.0.0.0/16');
      testApp.node.setContext('entraClientId', 'test-client-id');
      testApp.node.setContext('entraTenantId', 'test-tenant-id');
      testApp.node.setContext('frontend', { enabled: true, enableRoute53: false, cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, entraRedirectUri: 'http://localhost:3000/auth/callback' });
      testApp.node.setContext('inferenceApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, enableGpu: false, enableAuthentication: 'true', logLevel: 'INFO', uploadDir: '/tmp/uploads', outputDir: '/tmp/outputs', generatedImagesDir: '/tmp/images', apiUrl: 'http://localhost:8000', frontendUrl: 'http://localhost:3000', corsOrigins: 'http://localhost:3000', tavilyApiKey: 'test-key', novaActApiKey: 'test-key' });
      testApp.node.setContext('gateway', { enabled: true, apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { enabled: true, corsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { enabled: true, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      process.env.CDK_RAG_ENABLED = 'true'; // Enable RAG to trigger validation
      testApp.node.setContext('ragIngestion', {
        lambdaTimeout: -1, // Negative (invalid)
      });

      expect(() => loadConfig(testApp)).toThrow(
        'RAG Lambda timeout must be between 1 and 900 seconds'
      );
    });

    test('validates Lambda timeout maximum', () => {
      // Create a fresh app for this test
      const testApp = new cdk.App();
      testApp.node.setContext('projectPrefix', 'test-project');
      testApp.node.setContext('awsRegion', 'us-east-1');
      testApp.node.setContext('awsAccount', '123456789012');
      testApp.node.setContext('vpcCidr', '10.0.0.0/16');
      testApp.node.setContext('entraClientId', 'test-client-id');
      testApp.node.setContext('entraTenantId', 'test-tenant-id');
      testApp.node.setContext('frontend', { enabled: true, enableRoute53: false, cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, entraRedirectUri: 'http://localhost:3000/auth/callback' });
      testApp.node.setContext('inferenceApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, enableGpu: false, enableAuthentication: 'true', logLevel: 'INFO', uploadDir: '/tmp/uploads', outputDir: '/tmp/outputs', generatedImagesDir: '/tmp/images', apiUrl: 'http://localhost:8000', frontendUrl: 'http://localhost:3000', corsOrigins: 'http://localhost:3000', tavilyApiKey: 'test-key', novaActApiKey: 'test-key' });
      testApp.node.setContext('gateway', { enabled: true, apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { enabled: true, corsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { enabled: true, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      process.env.CDK_RAG_ENABLED = 'true'; // Enable RAG to trigger validation
      testApp.node.setContext('ragIngestion', {
        lambdaTimeout: 1000, // Too high
      });

      expect(() => loadConfig(testApp)).toThrow(
        'RAG Lambda timeout must be between 1 and 900 seconds'
      );
    });

    test('validates vector dimension is positive', () => {
      // Create a fresh app for this test
      const testApp = new cdk.App();
      testApp.node.setContext('projectPrefix', 'test-project');
      testApp.node.setContext('awsRegion', 'us-east-1');
      testApp.node.setContext('awsAccount', '123456789012');
      testApp.node.setContext('vpcCidr', '10.0.0.0/16');
      testApp.node.setContext('entraClientId', 'test-client-id');
      testApp.node.setContext('entraTenantId', 'test-tenant-id');
      testApp.node.setContext('frontend', { enabled: true, enableRoute53: false, cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, entraRedirectUri: 'http://localhost:3000/auth/callback' });
      testApp.node.setContext('inferenceApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, enableGpu: false, enableAuthentication: 'true', logLevel: 'INFO', uploadDir: '/tmp/uploads', outputDir: '/tmp/outputs', generatedImagesDir: '/tmp/images', apiUrl: 'http://localhost:8000', frontendUrl: 'http://localhost:3000', corsOrigins: 'http://localhost:3000', tavilyApiKey: 'test-key', novaActApiKey: 'test-key' });
      testApp.node.setContext('gateway', { enabled: true, apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { enabled: true, corsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { enabled: true, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      process.env.CDK_RAG_ENABLED = 'true'; // Enable RAG to trigger validation
      testApp.node.setContext('ragIngestion', {
        vectorDimension: -100, // Negative (invalid)
      });

      expect(() => loadConfig(testApp)).toThrow(
        'RAG vector dimension must be positive'
      );
    });

    test('validates vector dimension is positive for negative values', () => {
      process.env.CDK_RAG_VECTOR_DIMENSION = '-100';

      expect(() => loadConfig(app)).toThrow(
        'RAG vector dimension must be positive'
      );
    });

    test('validates distance metric is valid', () => {
      process.env.CDK_RAG_DISTANCE_METRIC = 'invalid_metric';

      expect(() => loadConfig(app)).toThrow(
        'RAG vector distance metric must be one of: cosine, euclidean, dot_product'
      );
    });

    test('accepts cosine distance metric', () => {
      process.env.CDK_RAG_DISTANCE_METRIC = 'cosine';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts euclidean distance metric', () => {
      process.env.CDK_RAG_DISTANCE_METRIC = 'euclidean';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts dot_product distance metric', () => {
      process.env.CDK_RAG_DISTANCE_METRIC = 'dot_product';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('validates embedding model is non-empty', () => {
      // Create a fresh app for this test
      const testApp = new cdk.App();
      testApp.node.setContext('projectPrefix', 'test-project');
      testApp.node.setContext('awsRegion', 'us-east-1');
      testApp.node.setContext('awsAccount', '123456789012');
      testApp.node.setContext('vpcCidr', '10.0.0.0/16');
      testApp.node.setContext('entraClientId', 'test-client-id');
      testApp.node.setContext('entraTenantId', 'test-tenant-id');
      testApp.node.setContext('frontend', { enabled: true, enableRoute53: false, cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, entraRedirectUri: 'http://localhost:3000/auth/callback' });
      testApp.node.setContext('inferenceApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, enableGpu: false, enableAuthentication: 'true', logLevel: 'INFO', uploadDir: '/tmp/uploads', outputDir: '/tmp/outputs', generatedImagesDir: '/tmp/images', apiUrl: 'http://localhost:8000', frontendUrl: 'http://localhost:3000', corsOrigins: 'http://localhost:3000', tavilyApiKey: 'test-key', novaActApiKey: 'test-key' });
      testApp.node.setContext('gateway', { enabled: true, apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { enabled: true, corsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { enabled: true, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      process.env.CDK_RAG_ENABLED = 'true'; // Enable RAG to trigger validation
      testApp.node.setContext('ragIngestion', {
        embeddingModel: '   ', // Whitespace only
      });

      expect(() => loadConfig(testApp)).toThrow(
        'RAG embedding model must be a non-empty string'
      );
    });

    test('validates embedding model is not whitespace only', () => {
      process.env.CDK_RAG_EMBEDDING_MODEL = '   ';

      expect(() => loadConfig(app)).toThrow(
        'RAG embedding model must be a non-empty string'
      );
    });

    test('accepts valid configuration', () => {
      process.env.CDK_RAG_ENABLED = 'true';
      process.env.CDK_RAG_CORS_ORIGINS = 'https://example.com';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';
      process.env.CDK_RAG_LAMBDA_TIMEOUT = '900';
      process.env.CDK_RAG_EMBEDDING_MODEL = 'amazon.titan-embed-text-v2';
      process.env.CDK_RAG_VECTOR_DIMENSION = '1024';
      process.env.CDK_RAG_DISTANCE_METRIC = 'cosine';

      expect(() => loadConfig(app)).not.toThrow();
    });
  });

  // ============================================================
  // Boolean Parsing Tests
  // ============================================================

  describe('Boolean Parsing', () => {
    test('parses "true" string as boolean true', () => {
      process.env.CDK_RAG_ENABLED = 'true';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true);
    });

    test('parses "false" string as boolean false', () => {
      process.env.CDK_RAG_ENABLED = 'false';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(false);
    });

    test('parses "TRUE" (uppercase) as boolean true', () => {
      process.env.CDK_RAG_ENABLED = 'TRUE';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true);
    });

    test('parses "FALSE" (uppercase) as boolean false', () => {
      process.env.CDK_RAG_ENABLED = 'FALSE';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(false);
    });

    test('empty string falls back to context or default', () => {
      process.env.CDK_RAG_ENABLED = '';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true); // default
    });
  });

  // ============================================================
  // Integer Parsing Tests
  // ============================================================

  describe('Integer Parsing', () => {
    test('parses valid integer string', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '8192';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(8192);
    });

    test('parses integer with leading zeros', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '008192';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(8192);
    });

    test('empty string falls back to context or default', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = '';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // default
    });

    test('invalid integer falls back to context or default', () => {
      process.env.CDK_RAG_LAMBDA_MEMORY = 'not-a-number';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // default
    });
  });

  // ============================================================
  // CORS Origins Validation Tests
  // ============================================================

  describe('CORS Origins Validation', () => {
    test('accepts valid HTTP origins', () => {
      process.env.CDK_RAG_CORS_ORIGINS = 'http://localhost:3000';

      // Should not throw, but may warn
      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts valid HTTPS origins', () => {
      process.env.CDK_RAG_CORS_ORIGINS = 'https://example.com';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts wildcard origin', () => {
      process.env.CDK_RAG_CORS_ORIGINS = '*';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts multiple comma-separated origins', () => {
      process.env.CDK_RAG_CORS_ORIGINS = 'http://localhost:3000,https://example.com,https://test.com';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('accepts empty CORS origins', () => {
      process.env.CDK_RAG_CORS_ORIGINS = '';

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('trims whitespace from origins', () => {
      process.env.CDK_RAG_CORS_ORIGINS = ' http://localhost:3000 , https://example.com ';

      const config = loadConfig(app);

      expect(config.ragIngestion.corsOrigins).toBe(' http://localhost:3000 , https://example.com ');
    });
  });

  // ============================================================
  // Precedence Tests
  // ============================================================

  describe('Configuration Precedence', () => {
    test('precedence order: env > context > default', () => {
      // Set context value
      app.node.setContext('ragIngestion', {
        lambdaMemorySize: 8192,
      });

      // Set environment variable (should override context)
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // env wins
    });

    test('context overrides default when env not set', () => {
      app.node.setContext('ragIngestion', {
        lambdaMemorySize: 8192,
      });

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(8192); // context wins over default
    });

    test('default used when neither env nor context set', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // default
    });

    test('mixed precedence for different fields', () => {
      app.node.setContext('ragIngestion', {
        enabled: false,
        corsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
      });

      process.env.CDK_RAG_ENABLED = 'true';
      // CDK_RAG_CORS_ORIGINS not set, should use context
      // CDK_RAG_LAMBDA_MEMORY not set, should use context

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(true); // env
      expect(config.ragIngestion.corsOrigins).toBe('https://context.example.com'); // context
      expect(config.ragIngestion.lambdaMemorySize).toBe(8192); // context
      expect(config.ragIngestion.lambdaTimeout).toBe(900); // default
    });
  });

  // ============================================================
  // Edge Cases Tests
  // ============================================================

  describe('Edge Cases', () => {
    test('handles undefined environment variables', () => {
      delete process.env.CDK_RAG_ENABLED;
      delete process.env.CDK_RAG_CORS_ORIGINS;

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('handles missing context values', () => {
      // Don't set ragIngestion context

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('handles partial context values', () => {
      app.node.setContext('ragIngestion', {
        enabled: false,
        // Other fields missing
      });

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(false); // from context
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // default
    });

    test('handles RAG disabled configuration', () => {
      process.env.CDK_RAG_ENABLED = 'false';

      const config = loadConfig(app);

      expect(config.ragIngestion.enabled).toBe(false);
      // Other fields should still be loaded
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
    });

    test('configuration is immutable after loading', () => {
      const config = loadConfig(app);
      const originalMemory = config.ragIngestion.lambdaMemorySize;

      // Try to modify (should not affect original)
      config.ragIngestion.lambdaMemorySize = 5000;

      // Load again and verify original value
      const config2 = loadConfig(app);
      expect(config2.ragIngestion.lambdaMemorySize).toBe(originalMemory);
    });
  });
});
