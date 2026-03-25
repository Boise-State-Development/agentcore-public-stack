import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';

/**
 * Runtime Configuration Interface
 * 
 * Defines the structure of the runtime configuration loaded from config.json.
 * This configuration is fetched at application startup and provides environment-specific
 * values without requiring environment-specific builds.
 */
export interface RuntimeConfig {
  /** App API backend URL (from ALB) */
  appApiUrl: string;

  /** Environment identifier (dev/staging/production/local) */
  environment: string;

  /** Application version from VERSION file (injected via config.json or environment fallback) */
  version: string;

  /** Cognito User Pool domain URL (e.g., https://myprefix.auth.us-east-1.amazoncognito.com) */
  cognitoDomainUrl: string;

  /** Cognito App Client ID */
  cognitoAppClientId: string;

  /** AWS region for Cognito (e.g., us-east-1) */
  cognitoRegion: string;

  /** Single inference API URL (replaces per-provider runtime endpoint resolution) */
  inferenceApiUrl: string;
}

/**
 * Configuration Service
 * 
 * Manages runtime configuration for the application. This service:
 * - Fetches configuration from /config.json at startup
 * - Validates configuration before storing
 * - Falls back to environment.ts for local development
 * - Provides reactive access to configuration via signals
 * 
 * The service is initialized via APP_INITIALIZER before the app bootstraps,
 * ensuring configuration is available to all services and components.
 * 
 * @example
 * ```typescript
 * // Inject the service
 * private readonly config = inject(ConfigService);
 * 
 * // Access configuration values
 * const apiUrl = this.config.appApiUrl();
 * const isProduction = this.config.environment() === 'production';
 * 
 * // Use in computed signals
 * readonly baseUrl = computed(() => this.config.appApiUrl());
 * ```
 */
@Injectable({
  providedIn: 'root'
})
export class ConfigService {
  private readonly http = inject(HttpClient);
  
  // Signal to store configuration
  private readonly config = signal<RuntimeConfig | null>(null);
  
  // Loading state tracking
  private readonly isLoaded = signal(false);
  private readonly loadError = signal<string | null>(null);
  
  /**
   * Computed signal for App API URL
   * Returns empty string if config not loaded
   */
  readonly appApiUrl = computed(() => this.config()?.appApiUrl ?? '');
  
  /**
   * Computed signal for environment identifier
   * Returns 'development' if config not loaded
   */
  readonly environment = computed(() => this.config()?.environment ?? 'development');
  
  /**
   * Computed signal for application version
   * Returns 'unknown' if config not loaded or version not set
   */
  readonly version = computed(() => this.config()?.version ?? 'unknown');

  /**
   * Computed signal for Cognito domain URL
   * Returns empty string if config not loaded
   */
  readonly cognitoDomainUrl = computed(() => this.config()?.cognitoDomainUrl ?? '');

  /**
   * Computed signal for Cognito App Client ID
   * Returns empty string if config not loaded
   */
  readonly cognitoAppClientId = computed(() => this.config()?.cognitoAppClientId ?? '');

  /**
   * Computed signal for Cognito region
   * Returns 'us-east-1' if config not loaded
   */
  readonly cognitoRegion = computed(() => this.config()?.cognitoRegion ?? 'us-east-1');

  /**
   * Computed signal for Inference API URL (single runtime endpoint).
   * URL-encodes the ARN portion of the path since AgentCore runtime ARNs
   * contain colons and slashes that break the URL if left raw.
   * Input:  https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn:aws:bedrock-agentcore:...
   * Output: https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3A...
   */
  readonly inferenceApiUrl = computed(() => {
    const raw = this.config()?.inferenceApiUrl ?? '';
    if (!raw) return '';

    const marker = '/runtimes/';
    const idx = raw.indexOf(marker);
    if (idx === -1) return raw;

    const base = raw.substring(0, idx + marker.length);
    const arn = raw.substring(idx + marker.length);
    return base + encodeURIComponent(arn);
  });
  
  /**
   * Read-only signal indicating if configuration has been loaded
   */
  readonly loaded = this.isLoaded.asReadonly();
  
  /**
   * Read-only signal containing any load error message
   */
  readonly error = this.loadError.asReadonly();
  
  /**
   * Load configuration from /config.json
   * 
   * This method is called by APP_INITIALIZER before app bootstrap.
   * It attempts to fetch runtime configuration from /config.json and falls back
   * to environment.ts values if the fetch fails.
   * 
   * The method:
   * 1. Attempts HTTP GET to /config.json
   * 2. Validates the configuration structure
   * 3. Stores valid configuration in signal
   * 4. Falls back to environment.ts on any error
   * 5. Sets loaded state to true
   * 
   * @returns Promise that resolves when configuration is loaded
   */
  async loadConfig(): Promise<void> {
    try {
      // Attempt to fetch runtime config
      const config = await firstValueFrom(
        this.http.get<RuntimeConfig>('/config.json')
      );
      
      // Validate configuration structure
      this.validateConfig(config);
      
      // Store validated configuration
      this.config.set(config);
      this.isLoaded.set(true);
      this.loadError.set(null);
      
      console.log('✅ Runtime configuration loaded:', config.environment);
      console.log('   App API URL:', config.appApiUrl);
      
    } catch (error) {
      // Log warning but don't fail - use fallback
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      console.warn('⚠️ Failed to load runtime config, using fallback:', errorMessage);
      
      // Fallback to environment.ts for local development
      const fallbackConfig: RuntimeConfig = {
        appApiUrl: environment.appApiUrl || 'http://localhost:8000',
        environment: environment.production ? 'production' : 'development',
        version: (environment as any).version || 'unknown',
        cognitoDomainUrl: (environment as any).cognitoDomainUrl || '',
        cognitoAppClientId: (environment as any).cognitoAppClientId || '',
        cognitoRegion: (environment as any).cognitoRegion || 'us-east-1',
        inferenceApiUrl: (environment as any).inferenceApiUrl || 'http://localhost:8001',
      };
      
      console.log('📋 Using fallback configuration from environment.ts');
      console.log('   App API URL:', fallbackConfig.appApiUrl);
      
      this.config.set(fallbackConfig);
      this.isLoaded.set(true);
      this.loadError.set(errorMessage);
    }
  }
  
  /**
   * Validate configuration has required fields and correct types
   * 
   * @param config - Configuration object to validate
   * @throws Error if configuration is invalid
   */
  private validateConfig(config: any): asserts config is RuntimeConfig {
    const errors: string[] = [];
    
    // Validate appApiUrl
    if (!config.appApiUrl || typeof config.appApiUrl !== 'string') {
      errors.push('appApiUrl is required and must be a string');
    } else {
      try {
        new URL(config.appApiUrl);
      } catch {
        errors.push(`appApiUrl is not a valid URL: "${config.appApiUrl}"`);
      }
    }
    
    // Validate environment
    if (!config.environment || typeof config.environment !== 'string') {
      errors.push('environment is required and must be a string');
    }
    
    // Throw error if validation failed
    if (errors.length > 0) {
      throw new Error(`Invalid configuration:\n${errors.map(e => `  - ${e}`).join('\n')}`);
    }
  }
  
  /**
   * Get a configuration value by key
   * 
   * Type-safe accessor for configuration values. Throws an error if
   * configuration is not loaded or the key doesn't exist.
   * 
   * @param key - Configuration key to retrieve
   * @returns Configuration value
   * @throws Error if configuration not loaded or key not found
   * 
   * @example
   * ```typescript
   * const apiUrl = configService.get('appApiUrl');
   * ```
   */
  get<K extends keyof RuntimeConfig>(key: K): RuntimeConfig[K] {
    const currentConfig = this.config();
    
    if (!currentConfig) {
      throw new Error('Configuration not loaded. Ensure APP_INITIALIZER has completed.');
    }
    
    const value = currentConfig[key];
    
    if (value === undefined) {
      throw new Error(`Configuration key '${key}' not found`);
    }
    
    return value;
  }
  
  /**
   * Get the full configuration object
   * 
   * @returns Current configuration or null if not loaded
   */
  getConfig(): RuntimeConfig | null {
    return this.config();
  }
  
  /**
   * Check if configuration is loaded and valid
   * 
   * @returns True if configuration is loaded
   */
  isConfigLoaded(): boolean {
    return this.isLoaded() && this.config() !== null;
  }
}
