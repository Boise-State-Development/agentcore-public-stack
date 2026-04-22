/**
 * Connector shape returned by the user-facing catalog endpoint.
 * Strips admin-only fields (ARN, callback URL, allow-list).
 */
export interface UserConnector {
  providerId: string;
  displayName: string;
  providerType: 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';
  iconName: string;
  scopes: string[];
}

export interface UserConnectorListResponse {
  connectors: UserConnector[];
}

/**
 * Inference-API response for `/connectors/{id}/initiate-consent`.
 * Exactly one of `connected` (true) or `authorizationUrl` (populated) will
 * be meaningful — `connected: false` with a URL is the consent path.
 */
export interface InitiateConsentResponse {
  connected: boolean;
  authorizationUrl: string | null;
}
