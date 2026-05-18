import * as cdk from 'aws-cdk-lib';
import { loadConfig, buildCorsOrigins, AppConfig } from '../lib/config';
import { buildMcpSandboxFrameAncestors } from '../lib/mcp-sandbox-stack';
import { createMockConfig } from './helpers/mock-config';

/**
 * Comprehensive CORS Configuration Tests
 *
 * These tests verify the two-layer CORS model:
 *   1. CDK_DOMAIN_NAME is ALWAYS auto-applied as https://{domainName}
 *   2. CDK_CORS_ORIGINS (or section-specific extras) are APPENDED
 *
 * localhost is NOT auto-included — use CDK_CORS_ORIGINS to add it for local dev.
 *
 * The flow:
 *   GitHub vars.CDK_DOMAIN_NAME
 *     → workflow job env → load-env.sh → --context domainName
 *     → config.ts: corsOrigins = "https://{domainName}" + extras
 *     → buildCorsOrigins(config, additionalOrigins?) → string[]
 *     → each stack uses the array for S3 CORS / container env vars
 */

describe('buildCorsOrigins', () => {
  // ============================================================
  // Layer 1: CDK_DOMAIN_NAME is always auto-applied
  // ============================================================

  describe('Layer 1: domainName auto-applied', () => {
    test('includes https://{domainName} when set via corsOrigins', () => {
      const config = createMockConfig({
        corsOrigins: 'https://example.com',
        domainName: 'example.com',
      });
      const origins = buildCorsOrigins(config);
      expect(origins).toContain('https://example.com');
    });

    test('does NOT auto-include localhost', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config);
      expect(origins).not.toContain('http://localhost:4200');
    });

    test('returns empty array when corsOrigins is empty and no extras', () => {
      const config = createMockConfig({ corsOrigins: '', domainName: undefined });
      const origins = buildCorsOrigins(config);
      expect(origins).toEqual([]);
    });

    test('localhost is included only when explicitly in corsOrigins', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com,http://localhost:4200' });
      const origins = buildCorsOrigins(config);
      expect(origins).toContain('http://localhost:4200');
      expect(origins).toContain('https://example.com');
    });
  });

  // ============================================================
  // Layer 2: Additional origins are appended
  // ============================================================

  describe('Layer 2: additional origins appended', () => {
    test('appends additionalOrigins parameter', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, 'https://extra.com');
      expect(origins).toContain('https://example.com');
      expect(origins).toContain('https://extra.com');
    });

    test('appends multiple comma-separated additional origins', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, 'https://a.com,https://b.com');
      expect(origins).toContain('https://a.com');
      expect(origins).toContain('https://b.com');
    });

    test('handles undefined additionalOrigins gracefully', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, undefined);
      expect(origins).toEqual(['https://example.com']);
    });

    test('handles empty string additionalOrigins', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, '');
      expect(origins).toEqual(['https://example.com']);
    });

    test('localhost can be added via additionalOrigins', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, 'http://localhost:4200');
      expect(origins).toContain('http://localhost:4200');
      expect(origins).toContain('https://example.com');
    });
  });

  // ============================================================
  // Deduplication
  // ============================================================

  describe('deduplication', () => {
    test('deduplicates when corsOrigins and additionalOrigins overlap', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com' });
      const origins = buildCorsOrigins(config, 'https://example.com');
      const count = origins.filter(o => o === 'https://example.com').length;
      expect(count).toBe(1);
    });

    test('deduplicates localhost when in both corsOrigins and additionalOrigins', () => {
      const config = createMockConfig({ corsOrigins: 'http://localhost:4200,https://example.com' });
      const origins = buildCorsOrigins(config, 'http://localhost:4200');
      const count = origins.filter(o => o === 'http://localhost:4200').length;
      expect(count).toBe(1);
    });
  });

  // ============================================================
  // Whitespace handling
  // ============================================================

  describe('whitespace handling', () => {
    test('trims whitespace from origins', () => {
      const config = createMockConfig({ corsOrigins: ' https://example.com , https://other.com ' });
      const origins = buildCorsOrigins(config);
      expect(origins).toContain('https://example.com');
      expect(origins).toContain('https://other.com');
    });

    test('filters out empty strings from splitting', () => {
      const config = createMockConfig({ corsOrigins: 'https://example.com,,,' });
      const origins = buildCorsOrigins(config);
      expect(origins).not.toContain('');
    });
  });
});

// ============================================================
// loadConfig corsOrigins derivation tests
// ============================================================

describe('loadConfig CORS derivation', () => {
  let app: cdk.App;
  let originalEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    originalEnv = { ...process.env };
    app = new cdk.App();
    app.node.setContext('projectPrefix', 'test-project');
    app.node.setContext('awsRegion', 'us-east-1');
    app.node.setContext('awsAccount', '123456789012');
    app.node.setContext('vpcCidr', '10.0.0.0/16');
    app.node.setContext('domainName', 'test.example.com');
    app.node.setContext('frontend', { enabled: true, cloudFrontPriceClass: 'PriceClass_100' });
    app.node.setContext('appApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
    app.node.setContext('inferenceApi', { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
    app.node.setContext('gateway', { enabled: true, apiType: 'REST', throttleRateLimit: 1000, throttleBurstLimit: 2000, enableWaf: false });
    app.node.setContext('assistants', { enabled: true });
    app.node.setContext('fileUpload', { enabled: true, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
    app.node.setContext('ragIngestion', { enabled: true, additionalCorsOrigins: '', lambdaMemorySize: 10240, lambdaTimeout: 900, embeddingModel: 'amazon.titan-embed-text-v2', vectorDimension: 1024, vectorDistanceMetric: 'cosine' });
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  test('corsOrigins includes domain when CDK_DOMAIN_NAME is set via context', () => {
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://test.example.com');
  });

  test('corsOrigins includes domain when CDK_DOMAIN_NAME is set via env var', () => {
    process.env.CDK_DOMAIN_NAME = 'env.example.com';
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://env.example.com');
  });

  test('CDK_DOMAIN_NAME env var takes precedence over context domainName', () => {
    process.env.CDK_DOMAIN_NAME = 'env.example.com';
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://env.example.com');
    expect(config.corsOrigins).not.toContain('https://test.example.com');
  });

  test('CDK_CORS_ORIGINS appends to domain-derived origin', () => {
    process.env.CDK_CORS_ORIGINS = 'https://extra.com';
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://test.example.com');
    expect(config.corsOrigins).toContain('https://extra.com');
  });

  test('CDK_CORS_ORIGINS does NOT replace domain-derived origin', () => {
    process.env.CDK_CORS_ORIGINS = 'https://only-this.com';
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://test.example.com');
    expect(config.corsOrigins).toContain('https://only-this.com');
  });

  test('corsOrigins is empty when no domain and no extras', () => {
    app.node.setContext('domainName', '');
    app.node.setContext('fileUpload', { enabled: false, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
    const config = loadConfig(app);
    expect(config.corsOrigins).toBe('');
  });

  test('context corsOrigins appends to domain (not replaces)', () => {
    app.node.setContext('corsOrigins', 'https://context-extra.com');
    const config = loadConfig(app);
    expect(config.corsOrigins).toContain('https://test.example.com');
    expect(config.corsOrigins).toContain('https://context-extra.com');
  });

  test('buildCorsOrigins with loaded config includes domain only (no auto-localhost)', () => {
    const config = loadConfig(app);
    const origins = buildCorsOrigins(config);
    expect(origins).toContain('https://test.example.com');
    expect(origins).not.toContain('http://localhost:4200');
  });

  test('buildCorsOrigins with section extras appends them', () => {
    app.node.setContext('ragIngestion', {
      enabled: true,
      additionalCorsOrigins: 'https://rag-extra.com',
      lambdaMemorySize: 10240, lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2', vectorDimension: 1024, vectorDistanceMetric: 'cosine',
    });
    const config = loadConfig(app);
    const origins = buildCorsOrigins(config, config.ragIngestion.additionalCorsOrigins);
    expect(origins).toContain('https://test.example.com');
    expect(origins).toContain('https://rag-extra.com');
  });

  test('real-world: alpha.boisestate.ai + poop.com global + pee.com inference-only', () => {
    process.env.CDK_DOMAIN_NAME = 'alpha.boisestate.ai';
    process.env.CDK_CORS_ORIGINS = 'https://poop.com';
    process.env.CDK_INFERENCE_API_CORS_ORIGINS = 'https://pee.com';
    const config = loadConfig(app);

    // Global origins (app-api, frontend, etc.)
    const globalOrigins = buildCorsOrigins(config);
    expect(globalOrigins).toContain('https://alpha.boisestate.ai');
    expect(globalOrigins).toContain('https://poop.com');
    expect(globalOrigins).not.toContain('https://pee.com');

    // Inference-api origins (global + section extra)
    const inferenceOrigins = buildCorsOrigins(config, config.inferenceApi.additionalCorsOrigins);
    expect(inferenceOrigins).toContain('https://alpha.boisestate.ai');
    expect(inferenceOrigins).toContain('https://poop.com');
    expect(inferenceOrigins).toContain('https://pee.com');
  });

  test('real-world: local dev with CDK_CORS_ORIGINS=http://localhost:4200', () => {
    app.node.setContext('domainName', '');
    app.node.setContext('fileUpload', { enabled: false, maxFileSizeBytes: 4194304, maxFilesPerMessage: 5, userQuotaBytes: 1073741824, retentionDays: 365 });
    process.env.CDK_CORS_ORIGINS = 'http://localhost:4200';
    const config = loadConfig(app);
    const origins = buildCorsOrigins(config);
    expect(origins).toEqual(['http://localhost:4200']);
  });
});

// ============================================================
// MCP Apps sandbox-proxy origin allowlisting (PR #1)
//
// The proxy is not a CORS consumer (buildCorsOrigins) — it is an origin
// whose CSP frame-ancestors must permit ONLY the SPA. Per the
// cors-deployment skill, every new env var that names this origin /
// allowlists it flows through this file: these tests pin that the proxy's
// framing allowlist is derived from the SAME domainName the CORS model is
// centred on, so the two allowlists can never silently drift apart.
// ============================================================

describe('MCP sandbox proxy frame-ancestors vs CORS domain', () => {
  test('frame-ancestors matches the domain-derived SPA CORS origin', () => {
    const config = createMockConfig({
      corsOrigins: 'https://alpha.example.com',
      domainName: 'alpha.example.com',
    });
    const corsOrigins = buildCorsOrigins(config);
    const frameAncestors = buildMcpSandboxFrameAncestors(
      config.domainName,
      config.mcpSandbox.extraFrameAncestors,
    );
    // The single SPA origin the CORS layer allows is exactly the single
    // origin permitted to frame the proxy.
    expect(corsOrigins).toContain('https://alpha.example.com');
    expect(frameAncestors).toBe('https://alpha.example.com');
  });

  test('never widens to * and denies framing when there is no SPA origin', () => {
    const config = createMockConfig({ corsOrigins: '', domainName: undefined });
    const frameAncestors = buildMcpSandboxFrameAncestors(
      config.domainName,
      config.mcpSandbox.extraFrameAncestors,
    );
    expect(frameAncestors).toBe("'none'");
    expect(frameAncestors).not.toContain('*');
  });

  test('extra frame ancestors (e.g. localhost SPA) are additive, like CDK_CORS_ORIGINS', () => {
    const config = createMockConfig({
      corsOrigins: 'https://alpha.example.com,http://localhost:4200',
      domainName: 'alpha.example.com',
      mcpSandbox: { enabled: true, extraFrameAncestors: ['http://localhost:4200'] },
    });
    const frameAncestors = buildMcpSandboxFrameAncestors(
      config.domainName,
      config.mcpSandbox.extraFrameAncestors,
    );
    expect(frameAncestors).toBe('https://alpha.example.com http://localhost:4200');
  });
});
