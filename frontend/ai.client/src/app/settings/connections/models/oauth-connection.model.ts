/**
 * OAuth connection models for user-facing connections UI.
 */

/** Connection status for user OAuth tokens */
export type OAuthConnectionStatus = 'connected' | 'expired' | 'revoked' | 'needs_reauth';

/** Supported OAuth provider types */
export type OAuthProviderType = 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';

/**
 * User's OAuth connection to a provider.
 * Returned from GET /oauth/connections
 */
export interface OAuthConnection {
  providerId: string;
  displayName: string;
  providerType: OAuthProviderType;
  iconName: string;
  status: OAuthConnectionStatus;
  connectedAt: string | null;
  needsReauth: boolean;
}

/**
 * Response from GET /oauth/connections
 */
export interface OAuthConnectionListResponse {
  connections: OAuthConnection[];
}

/**
 * Available OAuth provider for connection.
 * Returned from GET /oauth/providers (filtered by user roles)
 */
export interface OAuthProvider {
  providerId: string;
  displayName: string;
  providerType: OAuthProviderType;
  iconName: string;
  scopes: string[];
}

/**
 * Response from GET /oauth/providers
 */
export interface OAuthProviderListResponse {
  providers: OAuthProvider[];
  total: number;
}

/**
 * Response from GET /oauth/connect/{provider_id}
 */
export interface OAuthConnectResponse {
  authorizationUrl: string;
}
