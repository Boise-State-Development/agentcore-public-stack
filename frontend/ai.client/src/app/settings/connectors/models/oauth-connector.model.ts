/**
 * OAuth connector models for the user-facing Connectors UI.
 *
 * A "connector" is a single user-to-provider OAuth link surfaced in
 * /settings/connectors. The underlying backend endpoint still returns a
 * `connections` array — we translate that at the service layer.
 */

/** Connection status for a user's OAuth connector */
export type OAuthConnectorStatus = 'connected' | 'expired' | 'revoked' | 'needs_reauth';

/** Supported OAuth provider types */
export type OAuthProviderType = 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';

/**
 * A user's OAuth connector for a single provider.
 */
export interface OAuthConnector {
  providerId: string;
  displayName: string;
  providerType: OAuthProviderType;
  iconName: string;
  status: OAuthConnectorStatus;
  connectedAt: string | null;
  needsReauth: boolean;
}

/**
 * Response shape returned by {@link ConnectorsService.fetchConnectors}.
 */
export interface OAuthConnectorListResponse {
  connectors: OAuthConnector[];
}

/**
 * Available OAuth provider a user may connect to.
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
