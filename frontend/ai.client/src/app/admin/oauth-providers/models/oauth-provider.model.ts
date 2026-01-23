/**
 * OAuth provider type enumeration.
 */
export type OAuthProviderType = 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';

/**
 * OAuth Provider configuration.
 */
export interface OAuthProvider {
  /** Unique provider identifier (lowercase alphanumeric + underscore) */
  providerId: string;
  /** Human-readable display name */
  displayName: string;
  /** Provider type for preset configurations */
  providerType: OAuthProviderType;
  /** OAuth authorization endpoint URL */
  authorizationEndpoint: string;
  /** OAuth token endpoint URL */
  tokenEndpoint: string;
  /** OAuth client ID (public) */
  clientId: string;
  /** OAuth scopes to request */
  scopes: string[];
  /** AppRole IDs that can use this provider */
  allowedRoles: string[];
  /** Whether this provider is active */
  enabled: boolean;
  /** Icon name for UI display (heroicons) */
  iconName: string;
  /** ISO 8601 creation timestamp */
  createdAt: string;
  /** ISO 8601 update timestamp */
  updatedAt: string;
}

/**
 * Response model for listing OAuth providers.
 */
export interface OAuthProviderListResponse {
  providers: OAuthProvider[];
  total: number;
}

/**
 * Request model for creating a new OAuth provider.
 */
export interface OAuthProviderCreateRequest {
  /** Unique provider identifier (lowercase alphanumeric + underscore, 3-50 chars) */
  providerId: string;
  /** Human-readable display name (1-100 chars) */
  displayName: string;
  /** Provider type */
  providerType: OAuthProviderType;
  /** OAuth authorization endpoint URL */
  authorizationEndpoint: string;
  /** OAuth token endpoint URL */
  tokenEndpoint: string;
  /** OAuth client ID */
  clientId: string;
  /** OAuth client secret (only sent on create, never returned) */
  clientSecret: string;
  /** OAuth scopes to request */
  scopes: string[];
  /** AppRole IDs that can use this provider */
  allowedRoles?: string[];
  /** Whether this provider is active */
  enabled?: boolean;
  /** Icon name for UI display */
  iconName?: string;
}

/**
 * Request model for updating an OAuth provider.
 * All fields are optional for partial updates.
 */
export interface OAuthProviderUpdateRequest {
  /** Human-readable display name (1-100 chars) */
  displayName?: string;
  /** OAuth authorization endpoint URL */
  authorizationEndpoint?: string;
  /** OAuth token endpoint URL */
  tokenEndpoint?: string;
  /** OAuth client ID */
  clientId?: string;
  /** OAuth client secret (only set if updating) */
  clientSecret?: string;
  /** OAuth scopes to request */
  scopes?: string[];
  /** AppRole IDs that can use this provider */
  allowedRoles?: string[];
  /** Whether this provider is active */
  enabled?: boolean;
  /** Icon name for UI display */
  iconName?: string;
}

/**
 * Form data model for creating/editing an OAuth provider.
 */
export interface OAuthProviderFormData {
  providerId: string;
  displayName: string;
  providerType: OAuthProviderType;
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
  clientSecret: string;
  scopes: string;
  allowedRoles: string[];
  enabled: boolean;
  iconName: string;
}

/**
 * Preset configurations for common OAuth providers.
 */
export interface OAuthProviderPreset {
  type: OAuthProviderType;
  displayName: string;
  authorizationEndpoint: string;
  tokenEndpoint: string;
  defaultScopes: string[];
  iconName: string;
}

/**
 * Common OAuth provider presets.
 */
export const OAUTH_PROVIDER_PRESETS: OAuthProviderPreset[] = [
  {
    type: 'google',
    displayName: 'Google',
    authorizationEndpoint: 'https://accounts.google.com/o/oauth2/v2/auth',
    tokenEndpoint: 'https://oauth2.googleapis.com/token',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroCloud',
  },
  {
    type: 'microsoft',
    displayName: 'Microsoft',
    authorizationEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    tokenEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
    defaultScopes: ['openid', 'email', 'profile', 'offline_access'],
    iconName: 'heroCloud',
  },
  {
    type: 'github',
    displayName: 'GitHub',
    authorizationEndpoint: 'https://github.com/login/oauth/authorize',
    tokenEndpoint: 'https://github.com/login/oauth/access_token',
    defaultScopes: ['read:user', 'user:email'],
    iconName: 'heroCodeBracket',
  },
  {
    type: 'canvas',
    displayName: 'Canvas LMS',
    authorizationEndpoint: '', // User must configure
    tokenEndpoint: '', // User must configure
    defaultScopes: [],
    iconName: 'heroAcademicCap',
  },
  {
    type: 'custom',
    displayName: 'Custom Provider',
    authorizationEndpoint: '',
    tokenEndpoint: '',
    defaultScopes: [],
    iconName: 'heroLink',
  },
];

/**
 * Get preset configuration for a provider type.
 */
export function getProviderPreset(type: OAuthProviderType): OAuthProviderPreset | undefined {
  return OAUTH_PROVIDER_PRESETS.find(preset => preset.type === type);
}
