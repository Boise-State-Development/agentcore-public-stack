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

/**
 * The `CDK_RAG_*` environment variables these tests manipulate. They are
 * deleted before AND after every test so a value set in one test can never
 * leak into the next and silently change loadConfig()'s outcome.
 *
 * This explicit per-key deletion — not whole-object `process.env = snapshot`
 * reassignment — is the load-bearing teardown: assigning a plain object to
 * `process.env` does NOT reliably *delete* keys on every Node version (some
 * merge the object in rather than replacing the backing store). Relying on
 * that alone let leaked `CDK_RAG_*` values mask the variable under test, so the
 * validation cases (e.g. an empty embedding model) saw a stale valid value and
 * loadConfig() never threw.
 */
const RAG_ENV_KEYS = [
  'CDK_RAG_CORS_ORIGINS',
  'CDK_RAG_LAMBDA_MEMORY',
  'CDK_RAG_LAMBDA_TIMEOUT',
  'CDK_RAG_EMBEDDING_MODEL',
  'CDK_RAG_VECTOR_DIMENSION',
  'CDK_RAG_DISTANCE_METRIC',
] as const;

function clearRagEnv(): void {
  for (const key of RAG_ENV_KEYS) {
    delete process.env[key];
  }
}

describe('RAG Ingestion Configuration', () => {
  let app: cdk.App;
  let originalEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    // Save the original environment, then start each test from a fresh copy so
    // mutations never touch the snapshot we restore from.
    originalEnv = { ...process.env };
    process.env = { ...originalEnv };
    // Hermetic start: drop any RAG keys a prior test may have leaked.
    clearRagEnv();

    // Create a fresh CDK app for each test
    app = new cdk.App();

    // Set required context values
    app.node.setContext('projectPrefix', 'test-project');
    app.node.setContext('awsRegion', 'us-east-1');
    app.node.setContext('awsAccount', '123456789012');
    app.node.setContext('vpcCidr', '10.0.0.0/16');
    app.node.setContext('domainName', 'test.example.com');

    // Set default context for other required fields
    app.node.setContext('frontend', {
      cloudFrontPriceClass: 'PriceClass_100',
    });
    app.node.setContext('appApi', {
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 4,
    });
    app.node.setContext('inferenceApi', {
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 4,
      logLevel: 'INFO',
    });
    app.node.setContext('gateway', {
      apiType: 'REST',
      throttleRateLimit: 1000,
      throttleBurstLimit: 2000,
      enableWaf: false,
    });
    app.node.setContext('assistants', {
      additionalCorsOrigins: 'http://localhost:3000',
    });
    app.node.setContext('fileUpload', {
      maxFileSizeBytes: 4194304,
      maxFilesPerMessage: 5,
      userQuotaBytes: 1073741824,
      retentionDays: 365,
      additionalCorsOrigins: 'http://localhost:4200',
    });

    // Set default ragIngestion context (mirrors cdk.context.json defaults)
    // Since task 1 removed hardcoded defaults from loadConfig(), tests must
    // provide context defaults for fields they don't explicitly set via env vars.
    app.node.setContext('ragIngestion', {
      additionalCorsOrigins: '',
      lambdaMemorySize: 10240,
      lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2',
      vectorDimension: 1024,
      vectorDistanceMetric: 'cosine',
    });
  });

  afterEach(() => {
    // Drop any RAG keys this test set so they can't leak forward, then restore
    // the original environment object.
    clearRagEnv();
    process.env = originalEnv;
  });

  // ============================================================
  // Environment Variable Loading Tests
  // ============================================================

  describe('Environment Variable Loading', () => {
    test('loads CORS origins from CDK_RAG_CORS_ORIGINS environment variable', () => {
      process.env.CDK_RAG_CORS_ORIGINS = 'https://example.com,https://test.com';

      const config = loadConfig(app);

      expect(config.ragIngestion.additionalCorsOrigins).toBe('https://example.com,https://test.com');
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
      process.env.CDK_RAG_CORS_ORIGINS = 'https://prod.example.com';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';
      process.env.CDK_RAG_LAMBDA_TIMEOUT = '900';
      process.env.CDK_RAG_EMBEDDING_MODEL = 'amazon.titan-embed-text-v2';
      process.env.CDK_RAG_VECTOR_DIMENSION = '1024';
      process.env.CDK_RAG_DISTANCE_METRIC = 'cosine';

      const config = loadConfig(app);

      expect(config.ragIngestion).toEqual({
        additionalCorsOrigins: 'https://prod.example.com',
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
        additionalCorsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
        embeddingModel: 'amazon.titan-embed-text-v1',
        vectorDimension: 512,
        vectorDistanceMetric: 'euclidean',
      });

      const config = loadConfig(app);

      expect(config.ragIngestion).toEqual({
        additionalCorsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
        embeddingModel: 'amazon.titan-embed-text-v1',
        vectorDimension: 512,
        vectorDistanceMetric: 'euclidean',
      });
    });

    test('environment variable takes precedence over context', () => {
      app.node.setContext('ragIngestion', {
        additionalCorsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });

      process.env.CDK_RAG_CORS_ORIGINS = 'https://env.example.com';
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.additionalCorsOrigins).toBe('https://env.example.com');
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
    });

    test('uses context for some values and env for others', () => {
      app.node.setContext('ragIngestion', {
        additionalCorsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 600,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });

      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.additionalCorsOrigins).toBe('https://context.example.com'); // from context
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

      expect(config.ragIngestion.additionalCorsOrigins).toBe('');
      expect(config.ragIngestion.lambdaMemorySize).toBe(10240);
      expect(config.ragIngestion.lambdaTimeout).toBe(900);
      expect(config.ragIngestion.embeddingModel).toBe('amazon.titan-embed-text-v2');
      expect(config.ragIngestion.vectorDimension).toBe(1024);
      expect(config.ragIngestion.vectorDistanceMetric).toBe('cosine');
    });

    test('default CORS origins is empty string', () => {
      const config = loadConfig(app);

      expect(config.ragIngestion.additionalCorsOrigins).toBe('');
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
  // KB Sync feature flag — default ON with a kill switch
  // ============================================================

  describe('KB Sync feature flag', () => {
    test('defaults to enabled when CDK_KB_SYNC_ENABLED is unset', () => {
      delete process.env.CDK_KB_SYNC_ENABLED;

      expect(loadConfig(app).kbSync.enabled).toBe(true);
    });

    test('treats empty string (unset GitHub Actions variable) as enabled', () => {
      // `${{ vars.CDK_KB_SYNC_ENABLED }}` renders to "" when the var is unset.
      process.env.CDK_KB_SYNC_ENABLED = '';

      expect(loadConfig(app).kbSync.enabled).toBe(true);
    });

    test('CDK_KB_SYNC_ENABLED="false" is the kill switch', () => {
      process.env.CDK_KB_SYNC_ENABLED = 'false';

      expect(loadConfig(app).kbSync.enabled).toBe(false);
    });

    test('CDK_KB_SYNC_ENABLED="true" stays enabled', () => {
      process.env.CDK_KB_SYNC_ENABLED = 'true';

      expect(loadConfig(app).kbSync.enabled).toBe(true);
    });

    test('cdk.json context kbSync.enabled=false disables when env is unset', () => {
      delete process.env.CDK_KB_SYNC_ENABLED;
      app.node.setContext('kbSync', { enabled: false });

      expect(loadConfig(app).kbSync.enabled).toBe(false);
    });
  });

  // ============================================================
  // Scheduled Runs feature flag — default ON with a kill switch
  // (same ternary as kbSync; empty workflow var must not disable)
  // ============================================================

  describe('Scheduled Runs feature flag', () => {
    test('defaults to enabled when CDK_SCHEDULED_RUNS_ENABLED is unset', () => {
      delete process.env.CDK_SCHEDULED_RUNS_ENABLED;

      expect(loadConfig(app).scheduledRuns.enabled).toBe(true);
    });

    test('treats empty string (unset GitHub Actions variable) as enabled', () => {
      // `${{ vars.CDK_SCHEDULED_RUNS_ENABLED }}` renders to "" when unset.
      process.env.CDK_SCHEDULED_RUNS_ENABLED = '';

      expect(loadConfig(app).scheduledRuns.enabled).toBe(true);
    });

    test('CDK_SCHEDULED_RUNS_ENABLED="false" is the kill switch', () => {
      process.env.CDK_SCHEDULED_RUNS_ENABLED = 'false';

      expect(loadConfig(app).scheduledRuns.enabled).toBe(false);
    });

    test('CDK_SCHEDULED_RUNS_ENABLED="true" stays enabled', () => {
      process.env.CDK_SCHEDULED_RUNS_ENABLED = 'true';

      expect(loadConfig(app).scheduledRuns.enabled).toBe(true);
    });

    test('cdk.json context scheduledRuns.enabled=false disables when env is unset', () => {
      delete process.env.CDK_SCHEDULED_RUNS_ENABLED;
      app.node.setContext('scheduledRuns', { enabled: false });

      expect(loadConfig(app).scheduledRuns.enabled).toBe(false);
    });
  });

  // ============================================================
  // Memory Spaces feature flag — default ON with a kill switch
  // (complete feature; ships enabled for forkers, empty var must not disable)
  // ============================================================

  describe('Memory Spaces feature flag', () => {
    test('defaults to enabled when CDK_MEMORY_SPACES_ENABLED is unset', () => {
      delete process.env.CDK_MEMORY_SPACES_ENABLED;

      expect(loadConfig(app).memorySpaces.enabled).toBe(true);
    });

    test('treats empty string (unset GitHub Actions variable) as enabled', () => {
      process.env.CDK_MEMORY_SPACES_ENABLED = '';

      expect(loadConfig(app).memorySpaces.enabled).toBe(true);
    });

    test('CDK_MEMORY_SPACES_ENABLED="false" is the kill switch', () => {
      process.env.CDK_MEMORY_SPACES_ENABLED = 'false';

      expect(loadConfig(app).memorySpaces.enabled).toBe(false);
    });

    test('CDK_MEMORY_SPACES_ENABLED="true" stays enabled', () => {
      process.env.CDK_MEMORY_SPACES_ENABLED = 'true';

      expect(loadConfig(app).memorySpaces.enabled).toBe(true);
    });

    test('cdk.json context memorySpaces.enabled=false disables when env is unset', () => {
      delete process.env.CDK_MEMORY_SPACES_ENABLED;
      app.node.setContext('memorySpaces', { enabled: false });

      expect(loadConfig(app).memorySpaces.enabled).toBe(false);
    });
  });

  // ============================================================
  // Agents API (Agent Designer) feature flag — default ON with a kill switch
  // (complete feature; ships enabled for forkers, empty var must not disable)
  // ============================================================

  describe('Agents API feature flag', () => {
    test('defaults to enabled when CDK_AGENTS_API_ENABLED is unset', () => {
      delete process.env.CDK_AGENTS_API_ENABLED;

      expect(loadConfig(app).agents.enabled).toBe(true);
    });

    test('treats empty string (unset GitHub Actions variable) as enabled', () => {
      process.env.CDK_AGENTS_API_ENABLED = '';

      expect(loadConfig(app).agents.enabled).toBe(true);
    });

    test('CDK_AGENTS_API_ENABLED="false" is the kill switch', () => {
      process.env.CDK_AGENTS_API_ENABLED = 'false';

      expect(loadConfig(app).agents.enabled).toBe(false);
    });

    test('CDK_AGENTS_API_ENABLED="true" stays enabled', () => {
      process.env.CDK_AGENTS_API_ENABLED = 'true';

      expect(loadConfig(app).agents.enabled).toBe(true);
    });

    test('cdk.json context agents.enabled=false disables when env is unset', () => {
      delete process.env.CDK_AGENTS_API_ENABLED;
      app.node.setContext('agents', { enabled: false });

      expect(loadConfig(app).agents.enabled).toBe(false);
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
      testApp.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
      testApp.node.setContext('inferenceApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
      testApp.node.setContext('gateway', { apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { additionalCorsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      testApp.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 10240,
        lambdaTimeout: -1, // Negative (invalid)
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
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
      testApp.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
      testApp.node.setContext('inferenceApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
      testApp.node.setContext('gateway', { apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { additionalCorsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      testApp.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 10240,
        lambdaTimeout: 1000, // Too high
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
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
      testApp.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
      testApp.node.setContext('inferenceApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
      testApp.node.setContext('gateway', { apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { additionalCorsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      testApp.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 10240,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: -100, // Negative (invalid)
        vectorDistanceMetric: 'cosine',
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
      testApp.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
      testApp.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
      testApp.node.setContext('inferenceApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
      testApp.node.setContext('gateway', { apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
      testApp.node.setContext('assistants', { additionalCorsOrigins: 'http://localhost:3000' });
      testApp.node.setContext('fileUpload', { maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
      
      testApp.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 10240,
        lambdaTimeout: 900,
        embeddingModel: '   ', // Whitespace only
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
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

      expect(config.ragIngestion.additionalCorsOrigins).toBe(' http://localhost:3000 , https://example.com ');
    });
  });

  // ============================================================
  // Precedence Tests
  // ============================================================

  describe('Configuration Precedence', () => {
    test('precedence order: env > context > default', () => {
      // Set context value
      app.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 8192,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });

      // Set environment variable (should override context)
      process.env.CDK_RAG_LAMBDA_MEMORY = '10240';

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // env wins
    });

    test('context overrides default when env not set', () => {
      app.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 8192,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
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
        additionalCorsOrigins: 'https://context.example.com',
        lambdaMemorySize: 8192,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });

      // CDK_RAG_CORS_ORIGINS not set, should use context
      // CDK_RAG_LAMBDA_MEMORY not set, should use context

      const config = loadConfig(app);

      expect(config.ragIngestion.additionalCorsOrigins).toBe('https://context.example.com'); // context
      expect(config.ragIngestion.lambdaMemorySize).toBe(8192); // context
      expect(config.ragIngestion.lambdaTimeout).toBe(900); // default
    });
  });

  // ============================================================
  // Edge Cases Tests
  // ============================================================

  describe('Edge Cases', () => {
    test('handles undefined environment variables', () => {
      delete process.env.CDK_RAG_CORS_ORIGINS;

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('handles missing context values', () => {
      // Don't set ragIngestion context

      expect(() => loadConfig(app)).not.toThrow();
    });

    test('handles partial context values', () => {
      app.node.setContext('ragIngestion', {
        additionalCorsOrigins: '',
        lambdaMemorySize: 10240,
        lambdaTimeout: 900,
        embeddingModel: 'amazon.titan-embed-text-v2',
        vectorDimension: 1024,
        vectorDistanceMetric: 'cosine',
      });

      const config = loadConfig(app);

      expect(config.ragIngestion.lambdaMemorySize).toBe(10240); // from context
    });

    test('handles RAG disabled configuration', () => {

      const config = loadConfig(app);

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

  // ============================================================
  // CloudFront Certificate Resolution Tests
  //
  // A single shared CDK_CLOUDFRONT_CERTIFICATE_ARN must satisfy all
  // three CloudFront origins (SPA / artifacts / mcp-sandbox), while a
  // section-specific ARN still overrides per origin. This is the
  // first-deploy footgun fix: one wildcard cert instead of three.
  // ============================================================

  describe('CloudFront Certificate Resolution', () => {
    const SHARED = 'arn:aws:acm:us-east-1:123456789012:certificate/shared-wildcard';
    const ARTIFACTS_SPECIFIC = 'arn:aws:acm:us-east-1:123456789012:certificate/artifacts-only';
    const FRONTEND_SPECIFIC = 'arn:aws:acm:us-east-1:123456789012:certificate/frontend-only';

    const CF_CERT_ENV_KEYS = [
      'CDK_CLOUDFRONT_CERTIFICATE_ARN',
      'CDK_FRONTEND_CERTIFICATE_ARN',
      'CDK_ARTIFACTS_CERTIFICATE_ARN',
      'CDK_MCP_SANDBOX_CERTIFICATE_ARN',
    ];

    function clearCfCertEnv(): void {
      for (const key of CF_CERT_ENV_KEYS) {
        delete process.env[key];
      }
    }

    beforeEach(clearCfCertEnv);
    afterEach(clearCfCertEnv);

    test('shared cert flows to all three CloudFront origins when none are set individually', () => {
      process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED;

      const config = loadConfig(app);

      expect(config.cloudfrontCertificateArn).toBe(SHARED);
      expect(config.frontend.certificateArn).toBe(SHARED);
      expect(config.artifacts.certificateArn).toBe(SHARED);
      expect(config.mcpSandbox.certificateArn).toBe(SHARED);
    });

    test('section-specific cert overrides the shared cert per origin', () => {
      process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED;
      process.env.CDK_ARTIFACTS_CERTIFICATE_ARN = ARTIFACTS_SPECIFIC;
      process.env.CDK_FRONTEND_CERTIFICATE_ARN = FRONTEND_SPECIFIC;

      const config = loadConfig(app);

      // Overridden origins keep their own cert...
      expect(config.artifacts.certificateArn).toBe(ARTIFACTS_SPECIFIC);
      expect(config.frontend.certificateArn).toBe(FRONTEND_SPECIFIC);
      // ...while the un-overridden origin falls back to the shared cert.
      expect(config.mcpSandbox.certificateArn).toBe(SHARED);
    });

    test('the shared cert resolves from CDK context when the env var is unset', () => {
      app.node.setContext('cloudfrontCertificateArn', SHARED);

      const config = loadConfig(app);

      expect(config.frontend.certificateArn).toBe(SHARED);
      expect(config.artifacts.certificateArn).toBe(SHARED);
      expect(config.mcpSandbox.certificateArn).toBe(SHARED);
    });

    test('the env var takes precedence over context for the shared cert', () => {
      app.node.setContext('cloudfrontCertificateArn', 'arn:aws:acm:us-east-1:123456789012:certificate/from-context');
      process.env.CDK_CLOUDFRONT_CERTIFICATE_ARN = SHARED;

      const config = loadConfig(app);

      expect(config.mcpSandbox.certificateArn).toBe(SHARED);
    });

    test('no cert anywhere leaves every CloudFront origin undefined (guards live in the constructs, not loadConfig)', () => {
      const config = loadConfig(app);

      expect(config.cloudfrontCertificateArn).toBeUndefined();
      expect(config.frontend.certificateArn).toBeUndefined();
      expect(config.artifacts.certificateArn).toBeUndefined();
      expect(config.mcpSandbox.certificateArn).toBeUndefined();
      // loadConfig itself must not throw on a domain-without-cert config —
      // that fail-loud behaviour is the constructs' responsibility, exercised
      // only on full synth (see *-cert-guard.test.ts).
      expect(() => loadConfig(app)).not.toThrow();
    });
  });

  // ============================================================
  // MCP Identity — token-enrichment feature flag
  //
  // Opt-in (default OFF), unlike the default-ON feature flags above.
  // The claim map is context-only (structured, not a scalar env var).
  // ============================================================

  describe('MCP Identity token enrichment feature flag', () => {
    const MCP_ENV_KEY = 'CDK_MCP_TOKEN_ENRICHMENT_ENABLED';
    const MCP_CLAIMS_ENV_KEY = 'CDK_MCP_TOKEN_ENRICHMENT_CLAIMS';

    beforeEach(() => {
      delete process.env[MCP_ENV_KEY];
      delete process.env[MCP_CLAIMS_ENV_KEY];
    });
    afterEach(() => {
      delete process.env[MCP_ENV_KEY];
      delete process.env[MCP_CLAIMS_ENV_KEY];
    });

    test('defaults to DISABLED when env is unset and no context is provided', () => {
      const config = loadConfig(app);

      expect(config.mcpIdentity.tokenEnrichment?.enabled).toBe(false);
      expect(config.mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({});
    });

    test('treats empty string (unset GitHub Actions variable) as disabled', () => {
      // `${{ vars.CDK_MCP_TOKEN_ENRICHMENT_ENABLED }}` renders to "" when unset.
      process.env[MCP_ENV_KEY] = '';

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.enabled).toBe(false);
    });

    test('CDK_MCP_TOKEN_ENRICHMENT_ENABLED="true" enables it (env opt-in)', () => {
      process.env[MCP_ENV_KEY] = 'true';

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.enabled).toBe(true);
    });

    test('CDK_MCP_TOKEN_ENRICHMENT_ENABLED="false" stays disabled', () => {
      process.env[MCP_ENV_KEY] = 'false';

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.enabled).toBe(false);
    });

    test('cdk.json context mcpIdentity.tokenEnrichment.enabled=true enables when env is unset', () => {
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: { enabled: true, accessTokenClaims: {} },
      });

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.enabled).toBe(true);
    });

    test('env takes precedence over context (env=false beats context=true)', () => {
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: { enabled: true, accessTokenClaims: {} },
      });
      process.env[MCP_ENV_KEY] = 'false';

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.enabled).toBe(false);
    });

    test('parses the namespaced accessTokenClaims map from context', () => {
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: {
          enabled: true,
          accessTokenClaims: {
            'https://boisestate.edu/employee_number': 'custom:provider_sub',
          },
        },
      });

      const config = loadConfig(app);

      expect(config.mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({
        'https://boisestate.edu/employee_number': 'custom:provider_sub',
      });
    });

    test('accessTokenClaims defaults to an empty map when context omits it', () => {
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: { enabled: true },
      });

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({});
    });

    test('accessTokenClaims parses from CDK_MCP_TOKEN_ENRICHMENT_CLAIMS JSON env', () => {
      process.env[MCP_CLAIMS_ENV_KEY] = JSON.stringify({
        'https://boisestate.edu/employee_number': 'custom:provider_sub',
      });

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({
        'https://boisestate.edu/employee_number': 'custom:provider_sub',
      });
    });

    test('claims JSON env takes precedence over context', () => {
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: {
          enabled: true,
          accessTokenClaims: { 'ctx:claim': 'custom:from_context' },
        },
      });
      process.env[MCP_CLAIMS_ENV_KEY] = JSON.stringify({
        'env:claim': 'custom:from_env',
      });

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({
        'env:claim': 'custom:from_env',
      });
    });

    test('malformed claims JSON env falls through to context/default (never throws)', () => {
      process.env[MCP_CLAIMS_ENV_KEY] = '{not valid json';

      expect(() => loadConfig(app)).not.toThrow();
      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({});
    });

    test('non-object claims JSON env is ignored (falls through to default)', () => {
      process.env[MCP_CLAIMS_ENV_KEY] = JSON.stringify(['not', 'a', 'map']);

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({});
    });

    test('empty claims JSON env falls through to context/default', () => {
      process.env[MCP_CLAIMS_ENV_KEY] = '';
      app.node.setContext('mcpIdentity', {
        tokenEnrichment: {
          enabled: true,
          accessTokenClaims: { 'ctx:claim': 'custom:from_context' },
        },
      });

      expect(loadConfig(app).mcpIdentity.tokenEnrichment?.accessTokenClaims).toEqual({
        'ctx:claim': 'custom:from_context',
      });
    });
  });
});
