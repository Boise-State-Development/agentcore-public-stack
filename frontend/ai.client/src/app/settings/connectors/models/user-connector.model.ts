/**
 * Connector shape returned by the user-facing catalog endpoint.
 * Strips admin-only fields (ARN, callback URL, allow-list).
 */
export interface UserConnector {
  providerId: string;
  displayName: string;
  providerType: 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';
  iconName: string;
  /** Optional admin-uploaded icon (base64 data URL). Wins over `iconName`. */
  iconData?: string | null;
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

/**
 * Inference-API response for `GET /connectors/{id}/status`. Side-effect-free:
 * unlike initiate-consent, this never remembers a session_uri server-side
 * or hands back an authorization URL. Use it to render "Connected" badges
 * without committing the user to a consent flow.
 */
export interface ConnectorStatusResponse {
  connected: boolean;
}
