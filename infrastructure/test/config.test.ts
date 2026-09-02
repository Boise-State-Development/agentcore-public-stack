import * as cdk from 'aws-cdk-lib';
import { loadConfig, AppConfig,
  OBSERVABILITY_DEFAULT_AGENTCORE_ERROR_THRESHOLD,
  OBSERVABILITY_DEFAULT_ALB_TARGET_5XX_THRESHOLD,
  OBSERVABILITY_DEFAULT_DYNAMO_THROTTLE_THRESHOLD,
  OBSERVABILITY_DEFAULT_ECS_CPU_PERCENT,
  OBSERVABILITY_DEFAULT_ECS_MEMORY_PERCENT,
  OBSERVABILITY_DEFAULT_LAMBDA_DURATION_PERCENT_OF_TIMEOUT,
  OBSERVABILITY_DEFAULT_LAMBDA_ERROR_THRESHOLD,
  OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS,
  OBSERVABILITY_DEFAULT_P99_LATENCY_MS,
  OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE,
  OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RESERVOIR,
} from '../lib/config';

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

/**
 * The `CDK_MANAGED_KB_*` environment variables. Scrubbed before AND after every
 * test for the same reason as the RAG keys, but the stakes are higher here: all
 * three Managed_KB flags default to **false**, so a value leaking in from the
 * ambient environment or a prior test would flip a flag ON and the
 * "defaults to off" assertions would pass for the wrong reason — exactly the
 * silent-arming failure Requirement 19.8 exists to prevent.
 */
const MANAGED_KB_ENV_KEYS = [
  'CDK_MANAGED_KB_NEW_DEFAULT',
  'CDK_MANAGED_KB_MIGRATION_ENABLED',
  'CDK_MANAGED_KB_RECONCILER_ARMED',
  'CDK_MANAGED_KB_PER_OWNER_BYTES',
  'CDK_MANAGED_KB_PER_OWNER_ELEVATED_BYTES',
  'CDK_MANAGED_KB_PER_KB_CEILING_BYTES',
  'CDK_MANAGED_KB_RETENTION_WINDOW_DAYS',
] as const;

function clearManagedKbEnv(): void {
  for (const key of MANAGED_KB_ENV_KEYS) {
    delete process.env[key];
  }
}

/**
 * Scrubbed before AND after every test: these assert the defaults, so a leaked
 * value would make a "defaults to X" assertion pass while reading an override.
 */
const OBSERVABILITY_ENV_KEYS = [
  'CDK_OBSERVABILITY_ALARM_TOPIC_ENABLED',
  'CDK_OBSERVABILITY_LOG_RETENTION_DAYS',
  'CDK_OBSERVABILITY_ALB_TARGET_5XX_THRESHOLD',
  'CDK_OBSERVABILITY_ALB_P99_LATENCY_MS',
  'CDK_OBSERVABILITY_AGENTCORE_LATENCY_MS',
  'CDK_OBSERVABILITY_AGENTCORE_ERROR_THRESHOLD',
  'CDK_OBSERVABILITY_LAMBDA_ERROR_THRESHOLD',
  'CDK_OBSERVABILITY_LAMBDA_DURATION_PERCENT_OF_TIMEOUT',
  'CDK_OBSERVABILITY_DYNAMO_THROTTLE_THRESHOLD',
  'CDK_OBSERVABILITY_ECS_CPU_PERCENT',
  'CDK_OBSERVABILITY_ECS_MEMORY_PERCENT',
  'CDK_OBSERVABILITY_XRAY_SAMPLING_RATE',
  'CDK_OBSERVABILITY_XRAY_SAMPLING_RESERVOIR',
  'CDK_OBSERVABILITY_XRAY_INSIGHTS_NOTIFICATIONS',
  'CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED',
  'CDK_OBSERVABILITY_PROMPT_CACHE_AVOIDABLE_MISS_THRESHOLD',
  'CDK_OBSERVABILITY_PROMPT_CACHE_WASTED_USD_THRESHOLD',
  'CDK_OBSERVABILITY_PROMPT_CACHE_SESSION_WASTED_USD_THRESHOLD',
] as const;

function clearObservabilityEnv(): void {
  for (const key of OBSERVABILITY_ENV_KEYS) {
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
    clearManagedKbEnv();
    clearObservabilityEnv();

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
    clearManagedKbEnv();
    clearObservabilityEnv();
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
  // Managed_KB flags — default OFF, opt-in (the inverse posture of
  // every kill-switch flag above, which is the whole point of these
  // tests). Validates Requirements 19.1-19.5, 19.8, 12.2, 14.7, 15.11.
  // ============================================================

  describe('Managed_KB feature flags', () => {
    // --- Requirement 19.5: all three default to off ---

    test('newDefault defaults to false when CDK_MANAGED_KB_NEW_DEFAULT is unset', () => {
      delete process.env.CDK_MANAGED_KB_NEW_DEFAULT;

      expect(loadConfig(app).managedKb.newDefault).toBe(false);
    });

    test('migrationEnabled defaults to false when CDK_MANAGED_KB_MIGRATION_ENABLED is unset', () => {
      delete process.env.CDK_MANAGED_KB_MIGRATION_ENABLED;

      expect(loadConfig(app).managedKb.migrationEnabled).toBe(false);
    });

    test('reconcilerArmed defaults to false when CDK_MANAGED_KB_RECONCILER_ARMED is unset', () => {
      delete process.env.CDK_MANAGED_KB_RECONCILER_ARMED;

      expect(loadConfig(app).managedKb.reconcilerArmed).toBe(false);
    });

    // --- Requirement 19.8: empty string is OFF, not ON ---
    //
    // This is the case that actually bites. `${{ vars.CDK_MANAGED_KB_* }}`
    // renders to "" when the variable is unset, so every fork that never
    // configures these gets an empty string, not an absent variable. The
    // `X ? X !== 'false' : default` shape used by the default-ON flags would
    // read "" as the default — harmless when the default is on, and a silent
    // fleet migration when the default is off.

    test('treats empty-string newDefault (unset GitHub Actions variable) as OFF', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = '';

      expect(loadConfig(app).managedKb.newDefault).toBe(false);
    });

    test('treats empty-string migrationEnabled (unset GitHub Actions variable) as OFF', () => {
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = '';

      expect(loadConfig(app).managedKb.migrationEnabled).toBe(false);
    });

    test('treats empty-string reconcilerArmed (unset GitHub Actions variable) as OFF', () => {
      process.env.CDK_MANAGED_KB_RECONCILER_ARMED = '';

      expect(loadConfig(app).managedKb.reconcilerArmed).toBe(false);
    });

    test('an empty string leaves all three flags off simultaneously', () => {
      // The realistic deploy: a fork sets none of them, so the workflow
      // forwards three empty strings at once.
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = '';
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = '';
      process.env.CDK_MANAGED_KB_RECONCILER_ARMED = '';

      const { managedKb } = loadConfig(app);

      expect(managedKb.newDefault).toBe(false);
      expect(managedKb.migrationEnabled).toBe(false);
      expect(managedKb.reconcilerArmed).toBe(false);
    });

    // --- Requirements 19.1-19.3: each flag can be turned on ---

    test('CDK_MANAGED_KB_NEW_DEFAULT="true" turns newDefault on', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = 'true';

      expect(loadConfig(app).managedKb.newDefault).toBe(true);
    });

    test('CDK_MANAGED_KB_MIGRATION_ENABLED="true" turns migrationEnabled on', () => {
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = 'true';

      expect(loadConfig(app).managedKb.migrationEnabled).toBe(true);
    });

    test('CDK_MANAGED_KB_RECONCILER_ARMED="true" arms the reconciler', () => {
      process.env.CDK_MANAGED_KB_RECONCILER_ARMED = 'true';

      expect(loadConfig(app).managedKb.reconcilerArmed).toBe(true);
    });

    test('"1" and "0" are accepted as on and off', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = '1';
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = '0';

      const { managedKb } = loadConfig(app);

      expect(managedKb.newDefault).toBe(true);
      expect(managedKb.migrationEnabled).toBe(false);
    });

    test('an explicit "false" stays off', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = 'false';
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = 'false';
      process.env.CDK_MANAGED_KB_RECONCILER_ARMED = 'false';

      const { managedKb } = loadConfig(app);

      expect(managedKb.newDefault).toBe(false);
      expect(managedKb.migrationEnabled).toBe(false);
      expect(managedKb.reconcilerArmed).toBe(false);
    });

    test('an unrecognised flag value fails fast rather than guessing', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = 'yes';

      expect(() => loadConfig(app)).toThrow(/Invalid boolean value/);
    });

    // --- Requirement 19.4: the three flags are independently settable ---

    test('arming migrationEnabled alone leaves the other two off', () => {
      process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = 'true';

      const { managedKb } = loadConfig(app);

      expect(managedKb.migrationEnabled).toBe(true);
      expect(managedKb.newDefault).toBe(false);
      expect(managedKb.reconcilerArmed).toBe(false);
    });

    test('arming newDefault alone leaves the other two off', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = 'true';

      const { managedKb } = loadConfig(app);

      expect(managedKb.newDefault).toBe(true);
      expect(managedKb.migrationEnabled).toBe(false);
      expect(managedKb.reconcilerArmed).toBe(false);
    });

    test('arming reconcilerArmed alone leaves the other two off', () => {
      // Requirement 14.7: report-only is the initial deployed mode, so the
      // reconciler is the one flag an operator flips WITHOUT any migration
      // running. It must not drag the others on with it.
      process.env.CDK_MANAGED_KB_RECONCILER_ARMED = 'true';

      const { managedKb } = loadConfig(app);

      expect(managedKb.reconcilerArmed).toBe(true);
      expect(managedKb.newDefault).toBe(false);
      expect(managedKb.migrationEnabled).toBe(false);
    });

    // --- The load-env.sh --context chain ---
    //
    // build_cdk_context_params emits `--context managedKb.<flag>=...`, which
    // sets context["managedKb.<flag>"] — a FLAT dotted key, not a nested
    // object. A section reading only tryGetContext('managedKb')?.flag would
    // accept the CLI flag and silently ignore it.

    test('the flat dotted context key from --context is honoured', () => {
      delete process.env.CDK_MANAGED_KB_NEW_DEFAULT;
      app.node.setContext('managedKb.newDefault', 'true');

      expect(loadConfig(app).managedKb.newDefault).toBe(true);
    });

    test('a nested cdk.context.json managedKb object is honoured', () => {
      delete process.env.CDK_MANAGED_KB_MIGRATION_ENABLED;
      app.node.setContext('managedKb', { migrationEnabled: true });

      expect(loadConfig(app).managedKb.migrationEnabled).toBe(true);
    });

    test('the environment variable outranks the dotted context key', () => {
      process.env.CDK_MANAGED_KB_NEW_DEFAULT = 'false';
      app.node.setContext('managedKb.newDefault', 'true');

      expect(loadConfig(app).managedKb.newDefault).toBe(false);
    });

    // --- Requirement 12.2: Byte_Cap defaults by role tier ---
    //
    // Asserted as literal byte counts on purpose. Comparing against the
    // exported MANAGED_KB_* constants would be a tautology: the assertion
    // would follow the constant wherever it moved, so silently halving a cap
    // (or slipping a unit) would keep every test green.

    test('the standard per-owner Byte_Cap defaults to 100 MB', () => {
      expect(loadConfig(app).managedKb.perOwnerDefaultBytes).toBe(104857600);
    });

    test('the elevated per-owner Byte_Cap defaults to 1 GB', () => {
      expect(loadConfig(app).managedKb.perOwnerElevatedBytes).toBe(1073741824);
    });

    test('the per-knowledge-base ceiling defaults to 500 MB', () => {
      expect(loadConfig(app).managedKb.perKnowledgeBaseCeilingBytes).toBe(524288000);
    });

    test('the byte caps are expressed in bytes, not megabytes', () => {
      // A unit slip is the likeliest way these go wrong, and a 100-vs-100 MB
      // mixup is invisible to an equality check on the wrong constant. Every
      // cap must be a whole number of MiB and far larger than its MB count.
      const { managedKb } = loadConfig(app);

      for (const bytes of [
        managedKb.perOwnerDefaultBytes,
        managedKb.perOwnerElevatedBytes,
        managedKb.perKnowledgeBaseCeilingBytes,
      ]) {
        expect(bytes % (1024 * 1024)).toBe(0);
        expect(bytes).toBeGreaterThan(1024 * 1024);
      }
    });

    test('the tiers are ordered standard < per-KB ceiling < elevated', () => {
      // The ceiling sits between the tiers deliberately: it must bound a
      // single runaway corpus for an elevated owner, while still leaving a
      // standard owner's whole allowance usable by one knowledge base.
      const { managedKb } = loadConfig(app);

      expect(managedKb.perOwnerDefaultBytes).toBeLessThan(
        managedKb.perKnowledgeBaseCeilingBytes,
      );
      expect(managedKb.perKnowledgeBaseCeilingBytes).toBeLessThan(
        managedKb.perOwnerElevatedBytes,
      );
    });

    test('each Byte_Cap is overridable, resolvable by role tier', () => {
      // Requirement 12.2 requires all three to be configurable.
      process.env.CDK_MANAGED_KB_PER_OWNER_BYTES = '52428800';
      process.env.CDK_MANAGED_KB_PER_OWNER_ELEVATED_BYTES = '2147483648';
      process.env.CDK_MANAGED_KB_PER_KB_CEILING_BYTES = '262144000';

      const { managedKb } = loadConfig(app);

      expect(managedKb.perOwnerDefaultBytes).toBe(52428800);
      expect(managedKb.perOwnerElevatedBytes).toBe(2147483648);
      expect(managedKb.perKnowledgeBaseCeilingBytes).toBe(262144000);
    });

    test('a Byte_Cap override arrives via the flat dotted context key too', () => {
      delete process.env.CDK_MANAGED_KB_PER_OWNER_BYTES;
      app.node.setContext('managedKb.perOwnerDefaultBytes', '52428800');

      expect(loadConfig(app).managedKb.perOwnerDefaultBytes).toBe(52428800);
    });

    // --- Requirement 15.11: rollback retention window ---

    test('the retention window defaults to 30 days', () => {
      expect(loadConfig(app).managedKb.retentionWindowDays).toBe(30);
    });

    test('the default retention window satisfies the 30-day floor', () => {
      // Requirement 15.11 states "at least 30 days". Asserting the floor
      // separately from the exact default keeps the requirement checked even
      // if the shipped default is later raised.
      expect(loadConfig(app).managedKb.retentionWindowDays).toBeGreaterThanOrEqual(30);
    });

    test('the retention window is overridable', () => {
      process.env.CDK_MANAGED_KB_RETENTION_WINDOW_DAYS = '45';

      expect(loadConfig(app).managedKb.retentionWindowDays).toBe(45);
    });

    test('the retention window arrives via the flat dotted context key too', () => {
      delete process.env.CDK_MANAGED_KB_RETENTION_WINDOW_DAYS;
      app.node.setContext('managedKb.retentionWindowDays', '60');

      expect(loadConfig(app).managedKb.retentionWindowDays).toBe(60);
    });

    // --- Requirement 12.13: fleet-level alarm thresholds ---
    //
    // Per-owner Byte_Caps bound one user; only these two bound the account,
    // and the gap they cover is ~$169/month expected versus ~$15,000/month
    // permitted by the per-owner caps alone. Each is asserted through all
    // three legs of the precedence chain because the dotted-context leg is
    // the one that was missing: load-env.sh emits
    // `--context managedKb.storageAlarmGb=...`, which sets the FLAT key
    // context['managedKb.storageAlarmGb'], so a nested-only read accepts the
    // flag and ignores it — an operator raises a threshold, the CLI takes the
    // flag without complaint, and the alarm keeps firing at the old number.

    test('the fleet alarm thresholds have documented defaults', () => {
      const { managedKb } = loadConfig(app);

      expect(managedKb.storageAlarmGb).toBe(500);
      expect(managedKb.dailyCostAlarmUsd).toBe(100);
    });

    test('the fleet alarm thresholds are overridable by environment variable', () => {
      process.env.CDK_MANAGED_KB_STORAGE_ALARM_GB = '750';
      process.env.CDK_MANAGED_KB_DAILY_COST_ALARM_USD = '250';

      const { managedKb } = loadConfig(app);

      expect(managedKb.storageAlarmGb).toBe(750);
      expect(managedKb.dailyCostAlarmUsd).toBe(250);
    });

    test('the storage alarm threshold arrives via the flat dotted context key', () => {
      delete process.env.CDK_MANAGED_KB_STORAGE_ALARM_GB;
      app.node.setContext('managedKb.storageAlarmGb', '750');

      expect(loadConfig(app).managedKb.storageAlarmGb).toBe(750);
    });

    test('the daily cost alarm threshold arrives via the flat dotted context key', () => {
      delete process.env.CDK_MANAGED_KB_DAILY_COST_ALARM_USD;
      app.node.setContext('managedKb.dailyCostAlarmUsd', '250');

      expect(loadConfig(app).managedKb.dailyCostAlarmUsd).toBe(250);
    });

    test('the environment variable outranks the dotted key for both thresholds', () => {
      process.env.CDK_MANAGED_KB_STORAGE_ALARM_GB = '900';
      process.env.CDK_MANAGED_KB_DAILY_COST_ALARM_USD = '300';
      app.node.setContext('managedKb.storageAlarmGb', '750');
      app.node.setContext('managedKb.dailyCostAlarmUsd', '250');

      const { managedKb } = loadConfig(app);

      expect(managedKb.storageAlarmGb).toBe(900);
      expect(managedKb.dailyCostAlarmUsd).toBe(300);
    });

    test('a nested managedKb object still supplies both thresholds', () => {
      delete process.env.CDK_MANAGED_KB_STORAGE_ALARM_GB;
      delete process.env.CDK_MANAGED_KB_DAILY_COST_ALARM_USD;
      app.node.setContext('managedKb', { storageAlarmGb: 111, dailyCostAlarmUsd: 22 });

      const { managedKb } = loadConfig(app);

      expect(managedKb.storageAlarmGb).toBe(111);
      expect(managedKb.dailyCostAlarmUsd).toBe(22);
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

// ============================================================
// Observability Configuration
// ============================================================

/**
 * Defaults are asserted against their exported constants, and every field is
 * checked through the FLAT dotted context key — `--context observability.x=y`
 * sets context['observability.x'] and does NOT build a nested object, a trap
 * that has already cost this repo twice.
 */
describe('Observability Configuration', () => {
  let app: cdk.App;
  let originalEnv: NodeJS.ProcessEnv;

  /** Minimum context for loadConfig() to reach the observability section. */
  function setRequiredContext(a: cdk.App): void {
    a.node.setContext('projectPrefix', 'test-project');
    a.node.setContext('awsRegion', 'us-east-1');
    a.node.setContext('awsAccount', '123456789012');
    a.node.setContext('vpcCidr', '10.0.0.0/16');
    a.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
    a.node.setContext('appApi', {
      cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4,
    });
    a.node.setContext('ragIngestion', {
      lambdaMemorySize: 10240,
      lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2',
      vectorDimension: 1024,
      vectorDistanceMetric: 'cosine',
    });
  }

  beforeEach(() => {
    originalEnv = { ...process.env };
    process.env = { ...originalEnv };
    clearObservabilityEnv();
    app = new cdk.App();
    setRequiredContext(app);
  });

  afterEach(() => {
    clearObservabilityEnv();
    process.env = originalEnv;
  });

  describe('cost-conscious defaults', () => {
    test('log retention defaults to the exported constant', () => {
      expect(loadConfig(app).observability.logRetentionDays).toBe(
        OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS,
      );
    });

    // Was fixedRate 1.0 for any fork that never set `production`.
    test('X-Ray sampling defaults to 1%, not 100%', () => {
      const { xraySamplingRate } = loadConfig(app).observability;
      expect(xraySamplingRate).toBe(OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE);
      expect(xraySamplingRate).toBeLessThanOrEqual(0.05);
    });

    test('X-Ray reservoir defaults to 1 trace/sec', () => {
      expect(loadConfig(app).observability.xraySamplingReservoir).toBe(
        OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RESERVOIR,
      );
    });

    test('AgentCore APPLICATION_LOGS default to OFF', () => {
      expect(loadConfig(app).observability.agentCoreApplicationLogsEnabled).toBe(false);
    });

    test('X-Ray Insights notifications default to OFF', () => {
      expect(loadConfig(app).observability.xrayInsightsNotifications).toBe(false);
    });

    test('alarm topic defaults to ON', () => {
      expect(loadConfig(app).observability.alarmTopicEnabled).toBe(true);
    });

    test('latency floors are streaming-aware, well above a normal agent turn', () => {
      const obs = loadConfig(app).observability;
      expect(obs.agentCoreLatencyMs).toBe(OBSERVABILITY_DEFAULT_P99_LATENCY_MS);
      expect(obs.albP99LatencyMs).toBe(OBSERVABILITY_DEFAULT_P99_LATENCY_MS);
      expect(obs.agentCoreLatencyMs).toBeGreaterThan(30_000);
    });

    test('threshold and percentage defaults match their constants', () => {
      const obs = loadConfig(app).observability;
      expect(obs.albTarget5xxThreshold).toBe(OBSERVABILITY_DEFAULT_ALB_TARGET_5XX_THRESHOLD);
      expect(obs.agentCoreErrorThreshold).toBe(OBSERVABILITY_DEFAULT_AGENTCORE_ERROR_THRESHOLD);
      expect(obs.lambdaErrorThreshold).toBe(OBSERVABILITY_DEFAULT_LAMBDA_ERROR_THRESHOLD);
      expect(obs.lambdaDurationPercentOfTimeout).toBe(
        OBSERVABILITY_DEFAULT_LAMBDA_DURATION_PERCENT_OF_TIMEOUT,
      );
      expect(obs.dynamoThrottleThreshold).toBe(OBSERVABILITY_DEFAULT_DYNAMO_THROTTLE_THRESHOLD);
      expect(obs.ecsCpuPercent).toBe(OBSERVABILITY_DEFAULT_ECS_CPU_PERCENT);
      expect(obs.ecsMemoryPercent).toBe(OBSERVABILITY_DEFAULT_ECS_MEMORY_PERCENT);
    });
  });

  describe('environment variable overrides', () => {
    test('CDK_OBSERVABILITY_LOG_RETENTION_DAYS reaches config', () => {
      process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS = '90';
      expect(loadConfig(app).observability.logRetentionDays).toBe(90);
    });

    // parseInt('0.25') is 0, which would switch sampling off entirely.
    test('fractional X-Ray sampling rate survives parsing', () => {
      process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE = '0.25';
      expect(loadConfig(app).observability.xraySamplingRate).toBe(0.25);
    });

    test('booleans parse from env', () => {
      process.env.CDK_OBSERVABILITY_ALARM_TOPIC_ENABLED = 'false';
      process.env.CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED = 'true';
      const obs = loadConfig(app).observability;
      expect(obs.alarmTopicEnabled).toBe(false);
      expect(obs.agentCoreApplicationLogsEnabled).toBe(true);
    });

    test('every numeric field is settable from its env var', () => {
      process.env.CDK_OBSERVABILITY_ALB_TARGET_5XX_THRESHOLD = '1';
      process.env.CDK_OBSERVABILITY_ALB_P99_LATENCY_MS = '2000';
      process.env.CDK_OBSERVABILITY_AGENTCORE_LATENCY_MS = '3000';
      process.env.CDK_OBSERVABILITY_AGENTCORE_ERROR_THRESHOLD = '4';
      process.env.CDK_OBSERVABILITY_LAMBDA_ERROR_THRESHOLD = '5';
      process.env.CDK_OBSERVABILITY_LAMBDA_DURATION_PERCENT_OF_TIMEOUT = '60';
      process.env.CDK_OBSERVABILITY_DYNAMO_THROTTLE_THRESHOLD = '7';
      process.env.CDK_OBSERVABILITY_ECS_CPU_PERCENT = '65';
      process.env.CDK_OBSERVABILITY_ECS_MEMORY_PERCENT = '70';
      process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RESERVOIR = '9';

      const obs = loadConfig(app).observability;
      expect(obs.albTarget5xxThreshold).toBe(1);
      expect(obs.albP99LatencyMs).toBe(2000);
      expect(obs.agentCoreLatencyMs).toBe(3000);
      expect(obs.agentCoreErrorThreshold).toBe(4);
      expect(obs.lambdaErrorThreshold).toBe(5);
      expect(obs.lambdaDurationPercentOfTimeout).toBe(60);
      expect(obs.dynamoThrottleThreshold).toBe(7);
      expect(obs.ecsCpuPercent).toBe(65);
      expect(obs.ecsMemoryPercent).toBe(70);
      expect(obs.xraySamplingReservoir).toBe(9);
    });
  });

  describe('flat dotted context key (what --context actually sets)', () => {
    test('flat dotted key is honoured for a number', () => {
      app.node.setContext('observability.logRetentionDays', '90');
      expect(loadConfig(app).observability.logRetentionDays).toBe(90);
    });

    test('flat dotted key is honoured for a fractional rate', () => {
      app.node.setContext('observability.xraySamplingRate', '0.5');
      expect(loadConfig(app).observability.xraySamplingRate).toBe(0.5);
    });

    test('flat dotted key is honoured for a boolean', () => {
      app.node.setContext('observability.agentCoreApplicationLogsEnabled', 'true');
      app.node.setContext('observability.promptCacheAvoidableMissThreshold', '22');
      app.node.setContext('observability.promptCacheWastedUsdThreshold', '2.5');
      app.node.setContext('observability.promptCacheSessionWastedUsdThreshold', '23');
      expect(loadConfig(app).observability.agentCoreApplicationLogsEnabled).toBe(true);
    });

    test('every field is reachable via its flat dotted key', () => {
      app.node.setContext('observability.alarmTopicEnabled', 'false');
      app.node.setContext('observability.logRetentionDays', '7');
      app.node.setContext('observability.albTarget5xxThreshold', '11');
      app.node.setContext('observability.albP99LatencyMs', '12');
      app.node.setContext('observability.agentCoreLatencyMs', '13');
      app.node.setContext('observability.agentCoreErrorThreshold', '14');
      app.node.setContext('observability.lambdaErrorThreshold', '15');
      app.node.setContext('observability.lambdaDurationPercentOfTimeout', '16');
      app.node.setContext('observability.dynamoThrottleThreshold', '17');
      app.node.setContext('observability.ecsCpuPercent', '18');
      app.node.setContext('observability.ecsMemoryPercent', '19');
      app.node.setContext('observability.xraySamplingRate', '0.2');
      app.node.setContext('observability.xraySamplingReservoir', '21');
      app.node.setContext('observability.xrayInsightsNotifications', 'true');
      app.node.setContext('observability.agentCoreApplicationLogsEnabled', 'true');
      app.node.setContext('observability.promptCacheAvoidableMissThreshold', '22');
      app.node.setContext('observability.promptCacheWastedUsdThreshold', '2.5');
      app.node.setContext('observability.promptCacheSessionWastedUsdThreshold', '23');

      expect(loadConfig(app).observability).toEqual({
        alarmTopicEnabled: false,
        logRetentionDays: 7,
        albTarget5xxThreshold: 11,
        albP99LatencyMs: 12,
        agentCoreLatencyMs: 13,
        agentCoreErrorThreshold: 14,
        lambdaErrorThreshold: 15,
        lambdaDurationPercentOfTimeout: 16,
        dynamoThrottleThreshold: 17,
        ecsCpuPercent: 18,
        ecsMemoryPercent: 19,
        xraySamplingRate: 0.2,
        xraySamplingReservoir: 21,
        xrayInsightsNotifications: true,
        agentCoreApplicationLogsEnabled: true,
        promptCacheAvoidableMissThreshold: 22,
        promptCacheWastedUsdThreshold: 2.5,
        promptCacheSessionWastedUsdThreshold: 23,
      });
    });
  });

  describe('nested context object (cdk.context.json)', () => {
    test('nested object is honoured', () => {
      app.node.setContext('observability', {
        logRetentionDays: 365,
        xraySamplingRate: 0.1,
        alarmTopicEnabled: false,
      });
      const obs = loadConfig(app).observability;
      expect(obs.logRetentionDays).toBe(365);
      expect(obs.xraySamplingRate).toBe(0.1);
      expect(obs.alarmTopicEnabled).toBe(false);
    });

    test('unset fields in a nested object still take their defaults', () => {
      app.node.setContext('observability', { logRetentionDays: 365 });
      const obs = loadConfig(app).observability;
      expect(obs.logRetentionDays).toBe(365);
      expect(obs.xraySamplingRate).toBe(OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE);
    });
  });

  describe('precedence: env > flat dotted context > nested context > default', () => {
    test('env beats both context forms', () => {
      process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS = '7';
      app.node.setContext('observability.logRetentionDays', '90');
      app.node.setContext('observability', { logRetentionDays: 365 });
      expect(loadConfig(app).observability.logRetentionDays).toBe(7);
    });

    test('flat dotted context beats nested context', () => {
      app.node.setContext('observability.logRetentionDays', '90');
      app.node.setContext('observability', { logRetentionDays: 365 });
      expect(loadConfig(app).observability.logRetentionDays).toBe(90);
    });

    // An unset GitHub Actions variable arrives as the empty string.
    test('empty env var falls through to the default', () => {
      process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS = '';
      process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE = '';
      const obs = loadConfig(app).observability;
      expect(obs.logRetentionDays).toBe(OBSERVABILITY_DEFAULT_LOG_RETENTION_DAYS);
      expect(obs.xraySamplingRate).toBe(OBSERVABILITY_DEFAULT_XRAY_SAMPLING_RATE);
    });

    // false is a legitimate value, not "absent".
    test('an explicit false is not overwritten by the ON default', () => {
      process.env.CDK_OBSERVABILITY_ALARM_TOPIC_ENABLED = 'false';
      expect(loadConfig(app).observability.alarmTopicEnabled).toBe(false);
    });
  });

  describe('validation', () => {
    test('rejects a retention value CloudWatch does not accept', () => {
      process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS = '45';
      expect(() => loadConfig(app)).toThrow(/logRetentionDays/);
    });

    test('accepts every documented CloudWatch retention value', () => {
      for (const days of [1, 7, 30, 90, 365, 3653]) {
        const freshApp = new cdk.App();
        setRequiredContext(freshApp);
        process.env.CDK_OBSERVABILITY_LOG_RETENTION_DAYS = String(days);
        expect(loadConfig(freshApp).observability.logRetentionDays).toBe(days);
      }
    });

    // 5 instead of 0.05 is a 100x cost error; reject rather than clamp.
    test('rejects an X-Ray sampling rate given as a percentage', () => {
      process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE = '5';
      expect(() => loadConfig(app)).toThrow(/xraySamplingRate/);
    });

    test('rejects a negative X-Ray sampling rate', () => {
      process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE = '-0.1';
      expect(() => loadConfig(app)).toThrow(/xraySamplingRate/);
    });

    test('accepts the boundary sampling rates 0.0 and 1.0', () => {
      for (const rate of ['0', '1']) {
        const freshApp = new cdk.App();
        setRequiredContext(freshApp);
        process.env.CDK_OBSERVABILITY_XRAY_SAMPLING_RATE = rate;
        expect(() => loadConfig(freshApp)).not.toThrow();
      }
    });

    test('rejects out-of-range percentages', () => {
      const cases: Array<[string, string]> = [
        ['CDK_OBSERVABILITY_ECS_CPU_PERCENT', '101'],
        ['CDK_OBSERVABILITY_ECS_MEMORY_PERCENT', '0'],
        ['CDK_OBSERVABILITY_LAMBDA_DURATION_PERCENT_OF_TIMEOUT', '150'],
      ];
      for (const [key, value] of cases) {
        const freshApp = new cdk.App();
        setRequiredContext(freshApp);
        clearObservabilityEnv();
        process.env[key] = value;
        expect(() => loadConfig(freshApp)).toThrow(/Expected a percentage/);
      }
    });
  });
});
