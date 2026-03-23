/**
 * Environment Configuration - Local Development
 * 
 * This file contains localhost defaults for local development.
 * 
 * RUNTIME CONFIGURATION:
 * In production, the application loads configuration from /config.json at startup.
 * These values serve as FALLBACK only if config.json cannot be loaded.
 * 
 * Local Development Setup:
 * Option 1 (Recommended): Create public/config.json with local backend URLs
 * Option 2 (Fallback): Use these environment.ts values (config.json fetch will fail)
 * 
 * Fallback Behavior:
 * - If /config.json fetch fails, ConfigService automatically uses these values
 * - Allows local development without AWS infrastructure
 * - No configuration needed for typical local development workflow
 * 
 * Local Development Values:
 * - appApiUrl: http://localhost:8000 (App API backend)
 * - production: false (development mode)
 */
export const environment = {
    production: false,
    appApiUrl: 'http://localhost:8000',
    version: 'dev',
    cognitoDomainUrl: '',
    cognitoAppClientId: '',
    cognitoRegion: 'us-east-1',
    inferenceApiUrl: 'http://localhost:8001',
};
