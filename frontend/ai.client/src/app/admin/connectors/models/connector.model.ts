/**
 * Connector type enumeration.
 *
 * `canvas` routes through AgentCore's `CustomOauth2` vendor but is kept
 * distinct so the UI can surface Canvas-specific guidance if we add a
 * preset later. Today it is treated like `custom`.
 */
export type ConnectorType = 'google' | 'microsoft' | 'github' | 'canvas' | 'custom';

/**
 * Connector record as returned by the admin API.
 *
 * AgentCore Identity is authoritative for `clientId`, `clientSecret`, the
 * vendor-specific endpoint config, and `callbackUrl`. Our backend caches
 * the ARN and callback URL on the record for admin convenience.
 */
export interface Connector {
  providerId: string;
  displayName: string;
  providerType: ConnectorType;
  scopes: string[];
  allowedRoles: string[];
  enabled: boolean;
  iconName: string;
  credentialProviderArn?: string | null;
  callbackUrl?: string | null;
  /** Custom/Canvas only — OIDC discovery URL or explicit server metadata. */
  oauthDiscoveryUrl?: string | null;
  authorizationServerMetadata?: Record<string, unknown> | null;
  /**
   * Vendor-specific OAuth params merged into AgentCore Identity's
   * `customParameters` at request time. Examples: Google `hd=mycorp.com`
   * for Workspace domain restriction, `prompt=consent` for stricter UX.
   * Hardcoded vendor baselines (e.g. Google's `access_type=offline`)
   * always win on conflict.
   */
  customParameters?: Record<string, string> | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * Response model for listing connectors.
 *
 * The backend still returns the array under `providers` — we preserve the
 * field name to match the wire format exactly.
 */
export interface ConnectorListResponse {
  providers: Connector[];
  total: number;
}

/**
 * Create request. `clientId` and `clientSecret` are forwarded to AgentCore
 * Identity and are never stored in our DynamoDB. Custom/Canvas providers
 * must supply exactly one of `oauthDiscoveryUrl` or
 * `authorizationServerMetadata`.
 */
export interface ConnectorCreateRequest {
  providerId: string;
  displayName: string;
  providerType: ConnectorType;
  clientId: string;
  clientSecret: string;
  scopes: string[];
  allowedRoles?: string[];
  enabled?: boolean;
  iconName?: string;
  oauthDiscoveryUrl?: string;
  authorizationServerMetadata?: Record<string, unknown>;
  customParameters?: Record<string, string>;
}

/**
 * Update request. Credential rotation requires `clientId` and
 * `clientSecret` together; metadata-only edits leave them undefined.
 *
 * `customParameters: {}` explicitly clears all admin-supplied extras;
 * `undefined` leaves the existing value alone.
 */
export interface ConnectorUpdateRequest {
  displayName?: string;
  clientId?: string;
  clientSecret?: string;
  scopes?: string[];
  allowedRoles?: string[];
  enabled?: boolean;
  iconName?: string;
  oauthDiscoveryUrl?: string;
  authorizationServerMetadata?: Record<string, unknown>;
  customParameters?: Record<string, string>;
}

/**
 * Form data bound to the connector form. Scopes are a comma-separated
 * string for admin entry; parsed into `string[]` before submit.
 */
export interface ConnectorFormData {
  providerId: string;
  displayName: string;
  providerType: ConnectorType;
  clientId: string;
  clientSecret: string;
  scopes: string;
  allowedRoles: string[];
  enabled: boolean;
  iconName: string;
  oauthDiscoveryUrl: string;
  /**
   * Free-form `key=value` lines for admin-supplied custom OAuth parameters,
   * one per line. Parsed to `Record<string, string>` before submit.
   */
  customParameters: string;
}

/**
 * Preset configuration for the connector picker. Endpoints are owned by
 * AgentCore Identity and not configurable here.
 */
export interface ConnectorPreset {
  type: ConnectorType;
  displayName: string;
  defaultScopes: string[];
  iconName: string;
  /** Optional hint shown to the admin when selecting the preset. */
  hint?: string;
}

export const CONNECTOR_PRESETS: ConnectorPreset[] = [
  {
    type: 'google',
    displayName: 'Google',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroCloud',
  },
  {
    type: 'microsoft',
    displayName: 'Microsoft',
    defaultScopes: ['openid', 'email', 'profile', 'offline_access'],
    iconName: 'heroCloud',
  },
  {
    type: 'github',
    displayName: 'GitHub',
    defaultScopes: ['read:user', 'user:email'],
    iconName: 'heroCodeBracket',
  },
  {
    type: 'custom',
    displayName: 'Custom (OIDC)',
    defaultScopes: [],
    iconName: 'heroLink',
    hint: 'Requires an OpenID Connect discovery URL',
  },
];

export function getConnectorPreset(type: ConnectorType): ConnectorPreset | undefined {
  return CONNECTOR_PRESETS.find(preset => preset.type === type);
}

/**
 * True when the provider type needs an OIDC discovery URL.
 */
export function requiresDiscovery(type: ConnectorType): boolean {
  return type === 'custom' || type === 'canvas';
}
