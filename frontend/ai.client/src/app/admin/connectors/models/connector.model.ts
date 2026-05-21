/**
 * Connector type enumeration.
 *
 * `canvas` routes through AgentCore's `CustomOauth2` vendor but is kept
 * distinct so the UI can surface Canvas-specific guidance if we add a
 * preset later. Today it is treated like `custom`.
 *
 * `slack`, `salesforce`, and `zoom` are first-class AgentCore Identity
 * vendors — endpoints + provider defaults are pre-configured by AgentCore,
 * so admins only supply credentials and scopes (no discovery URL).
 */
export type ConnectorType =
  | 'google'
  | 'microsoft'
  | 'github'
  | 'slack'
  | 'salesforce'
  | 'zoom'
  | 'canvas'
  | 'custom';

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
  /**
   * Optional admin-uploaded icon as a base64 data URL. When set, frontends
   * prefer this over `iconName`. Stored inline on the provider record.
   */
  iconData?: string | null;
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
  /**
   * File-source adapter key (e.g. `google-drive`) mapping this connector to
   * a file source. When set, the connector appears as a file source in the
   * assistant editor. Null/absent means it is not a file source.
   */
  fileSourceAdapterId?: string | null;
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
  /** Optional admin-uploaded icon as a base64 data URL. */
  iconData?: string;
  oauthDiscoveryUrl?: string;
  authorizationServerMetadata?: Record<string, unknown>;
  customParameters?: Record<string, string>;
  /** File-source adapter key; omit when the connector is not a file source. */
  fileSourceAdapterId?: string;
}

/**
 * Update request. Credential rotation requires `clientId` and
 * `clientSecret` together; metadata-only edits leave them undefined.
 *
 * `customParameters: {}` explicitly clears all admin-supplied extras;
 * `undefined` leaves the existing value alone. `iconData: ""` clears any
 * uploaded icon (frontends fall back to `iconName`); `undefined` leaves it.
 */
export interface ConnectorUpdateRequest {
  displayName?: string;
  clientId?: string;
  clientSecret?: string;
  scopes?: string[];
  allowedRoles?: string[];
  enabled?: boolean;
  iconName?: string;
  iconData?: string;
  oauthDiscoveryUrl?: string;
  authorizationServerMetadata?: Record<string, unknown>;
  customParameters?: Record<string, string>;
  /**
   * `""` clears the file-source mapping (the connector stops being a file
   * source); a populated adapter key sets it; `undefined` leaves it alone.
   */
  fileSourceAdapterId?: string;
}

/**
 * A file-source adapter shipped in the backend registry, as returned by
 * `GET /admin/file-source-adapters`. Read-only — adapters are code, not
 * config; an admin can only map a connector to an existing one.
 */
export interface FileSourceAdapter {
  key: string;
  displayName: string;
  icon: string;
  /** OAuth provider types this adapter may be mapped to. */
  compatibleProviderTypes: ConnectorType[];
  /** OAuth scopes the connector must grant for the adapter to work. */
  requiredScopes: string[];
}

export interface FileSourceAdapterListResponse {
  adapters: FileSourceAdapter[];
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
 *
 * `defaultScopes` and `defaultCustomParameters` populate the form when the
 * admin clicks a preset. `scopesPlaceholder` and `customParametersPlaceholder`
 * are vendor-relevant examples shown when the field is empty (e.g. after
 * the admin clears one to type their own).
 */
export interface ConnectorPreset {
  type: ConnectorType;
  displayName: string;
  defaultScopes: string[];
  defaultCustomParameters?: Record<string, string>;
  iconName: string;
  scopesPlaceholder?: string;
  customParametersPlaceholder?: string;
  /** Optional hint shown to the admin when selecting the preset. */
  hint?: string;
}

export const CONNECTOR_PRESETS: ConnectorPreset[] = [
  {
    type: 'google',
    displayName: 'Google',
    // No defaults — Google connectors are too multi-purpose to pre-pick
    // (Calendar / Gmail / Drive / Docs all need different scopes, and the
    // OIDC-only `openid email profile` set doesn't let an agent do
    // anything useful). The placeholder shows the URL format so admins
    // know what to type.
    defaultScopes: [],
    iconName: 'heroCloud',
    scopesPlaceholder:
      'openid, email, profile, https://www.googleapis.com/auth/calendar.readonly',
    customParametersPlaceholder: 'hd=mycompany.com\nprompt=consent',
  },
  {
    type: 'microsoft',
    displayName: 'Microsoft',
    defaultScopes: ['openid', 'email', 'profile', 'offline_access'],
    iconName: 'heroCloud',
    scopesPlaceholder:
      'openid, email, profile, offline_access, User.Read, Calendars.Read',
    customParametersPlaceholder: 'domain_hint=mycompany.com\nprompt=consent',
  },
  {
    type: 'github',
    displayName: 'GitHub',
    defaultScopes: ['read:user', 'user:email'],
    iconName: 'heroCodeBracket',
    scopesPlaceholder: 'read:user, user:email, repo',
  },
  {
    type: 'slack',
    displayName: 'Slack',
    defaultScopes: ['chat:write', 'channels:read', 'users:read'],
    iconName: 'heroChatBubbleLeftRight',
    scopesPlaceholder:
      'chat:write, channels:read, channels:history, users:read, files:read',
    customParametersPlaceholder: 'team=T0123456789',
  },
  {
    type: 'salesforce',
    displayName: 'Salesforce',
    defaultScopes: ['api', 'refresh_token', 'offline_access', 'id', 'openid'],
    iconName: 'heroCloud',
    scopesPlaceholder:
      'api, refresh_token, offline_access, id, openid, lightning, content',
    customParametersPlaceholder: 'prompt=login\nlogin_hint=user@mycompany.com',
  },
  {
    type: 'zoom',
    displayName: 'Zoom',
    defaultScopes: ['user:read:user', 'meeting:read:meeting'],
    iconName: 'heroVideoCamera',
    scopesPlaceholder:
      'user:read:user, meeting:read:meeting, recording:read:recording',
  },
  {
    type: 'custom',
    displayName: 'Custom (OIDC)',
    defaultScopes: [],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile',
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
