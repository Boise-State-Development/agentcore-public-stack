/**
 * Connector type enumeration.
 *
 * `canvas` routes through AgentCore's `CustomOauth2` vendor but is kept
 * distinct so the UI can surface Canvas-specific guidance if we add a
 * preset later. Today it is treated like `custom`.
 *
 * Every type other than `canvas` and `custom` is a first-class AgentCore
 * Identity vendor — endpoints + provider defaults are pre-configured by
 * AgentCore, so admins only supply credentials and scopes (no discovery URL).
 * The tenant-hosted ones (`TENANT_CONNECTOR_TYPES`) also need the tenant's
 * issuer and endpoints, which the form collects as `authorizationServerMetadata`.
 */
export type ConnectorType =
  | 'google'
  | 'microsoft'
  | 'github'
  | 'slack'
  | 'salesforce'
  | 'zoom'
  | 'atlassian'
  | 'linkedin'
  | 'x'
  | 'facebook'
  | 'yandex'
  | 'reddit'
  | 'twitch'
  | 'spotify'
  | 'dropbox'
  | 'notion'
  | 'hubspot'
  | 'okta'
  | 'auth0'
  | 'cognito'
  | 'cyberark'
  | 'fusionauth'
  | 'onelogin'
  | 'ping'
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
   * Microsoft only — the Entra directory (tenant) ID or verified domain that
   * consent is sent to. Needed for a single-tenant app registration.
   */
  tenantId?: string | null;
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
  /**
   * Export-target adapter key (e.g. `google-drive`) mapping this connector to
   * a destination. When set, the connector appears in the "Save to…" dialog
   * as a place to save a conversation. Null/absent means it is not an export
   * target. The write-direction mirror of `fileSourceAdapterId`.
   */
  exportTargetAdapterId?: string | null;
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
  /** Microsoft only; omit to use AgentCore's vendor default. */
  tenantId?: string;
  customParameters?: Record<string, string>;
  /** File-source adapter key; omit when the connector is not a file source. */
  fileSourceAdapterId?: string;
  /** Export-target adapter key; omit when the connector is not an export target. */
  exportTargetAdapterId?: string;
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
  /**
   * Microsoft only. `""` clears the tenant; `undefined` leaves it alone. A
   * real change must ride a credential rotation, like a discovery change.
   */
  tenantId?: string;
  customParameters?: Record<string, string>;
  /**
   * `""` clears the file-source mapping (the connector stops being a file
   * source); a populated adapter key sets it; `undefined` leaves it alone.
   */
  fileSourceAdapterId?: string;
  /**
   * `""` clears the export-target mapping (the connector stops being an export
   * target); a populated adapter key sets it; `undefined` leaves it alone.
   */
  exportTargetAdapterId?: string;
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
 * An export-target adapter shipped in the backend registry, as returned by
 * `GET /admin/export-target-adapters`. Read-only — adapters are code, not
 * config; an admin can only map a connector to an existing one. The
 * write-direction mirror of {@link FileSourceAdapter}.
 */
export interface ExportTargetAdapter {
  key: string;
  displayName: string;
  icon: string;
  /** OAuth provider types this adapter may be mapped to. */
  compatibleProviderTypes: ConnectorType[];
  /** OAuth scopes the connector must grant for the adapter to work. */
  requiredScopes: string[];
  /** Output formats this destination can produce (e.g. `google_doc`). */
  supportedFormats: string[];
}

export interface ExportTargetAdapterListResponse {
  adapters: ExportTargetAdapter[];
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
/** How the picker groups presets. */
export type ConnectorPresetCategory = 'workspace' | 'social' | 'identity' | 'custom';

export interface ConnectorPresetGroup {
  category: ConnectorPresetCategory;
  label: string;
  description: string;
}

/** Picker sections, in display order. */
export const CONNECTOR_PRESET_GROUPS: readonly ConnectorPresetGroup[] = [
  {
    category: 'workspace',
    label: 'Workspace and developer tools',
    description: 'Productivity suites, collaboration and engineering platforms.',
  },
  {
    category: 'social',
    label: 'Social and media',
    description: 'Consumer networks and media services.',
  },
  {
    category: 'identity',
    label: 'Identity providers',
    description: 'These run in your own tenant, so you will enter your issuer and endpoints.',
  },
  {
    category: 'custom',
    label: 'Anything else',
    description: 'Any OpenID Connect provider with a discovery URL.',
  },
];

export interface ConnectorPreset {
  type: ConnectorType;
  category: ConnectorPresetCategory;
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
    category: 'workspace',
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
    category: 'workspace',
    displayName: 'Microsoft',
    defaultScopes: ['openid', 'email', 'profile', 'offline_access'],
    iconName: 'heroCloud',
    scopesPlaceholder:
      'openid, email, profile, offline_access, User.Read, Calendars.Read',
    customParametersPlaceholder: 'domain_hint=mycompany.com\nprompt=consent',
  },
  {
    type: 'github',
    category: 'workspace',
    displayName: 'GitHub',
    defaultScopes: ['read:user', 'user:email'],
    iconName: 'heroCodeBracket',
    scopesPlaceholder: 'read:user, user:email, repo',
  },
  {
    type: 'slack',
    category: 'workspace',
    displayName: 'Slack',
    defaultScopes: ['chat:write', 'channels:read', 'users:read'],
    iconName: 'heroChatBubbleLeftRight',
    scopesPlaceholder:
      'chat:write, channels:read, channels:history, users:read, files:read',
    customParametersPlaceholder: 'team=T0123456789',
  },
  {
    type: 'salesforce',
    category: 'workspace',
    displayName: 'Salesforce',
    defaultScopes: ['api', 'refresh_token', 'offline_access', 'id', 'openid'],
    iconName: 'heroCloud',
    scopesPlaceholder:
      'api, refresh_token, offline_access, id, openid, lightning, content',
    customParametersPlaceholder: 'prompt=login\nlogin_hint=user@mycompany.com',
  },
  {
    type: 'zoom',
    category: 'workspace',
    displayName: 'Zoom',
    defaultScopes: ['user:read:user', 'meeting:read:meeting'],
    iconName: 'heroVideoCamera',
    scopesPlaceholder:
      'user:read:user, meeting:read:meeting, recording:read:recording',
  },
  {
    type: 'atlassian',
    category: 'workspace',
    displayName: 'Atlassian',
    defaultScopes: ['read:me', 'offline_access'],
    iconName: 'heroCloud',
    scopesPlaceholder: 'read:me, offline_access, read:jira-work, write:jira-work, read:confluence-content.all',
  },
  {
    type: 'linkedin',
    category: 'social',
    displayName: 'LinkedIn',
    defaultScopes: ['openid', 'profile', 'email'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, profile, email, w_member_social',
  },
  {
    type: 'x',
    category: 'social',
    displayName: 'X',
    defaultScopes: ['users.read', 'tweet.read', 'offline.access'],
    iconName: 'heroChatBubbleLeftRight',
    scopesPlaceholder: 'users.read, tweet.read, tweet.write, offline.access',
  },
  {
    type: 'facebook',
    category: 'social',
    displayName: 'Facebook',
    defaultScopes: ['public_profile', 'email'],
    iconName: 'heroLink',
    scopesPlaceholder: 'public_profile, email',
  },
  {
    type: 'yandex',
    category: 'social',
    displayName: 'Yandex',
    defaultScopes: [],
    iconName: 'heroLink',
    scopesPlaceholder: 'login:info, login:email',
  },
  {
    type: 'reddit',
    category: 'social',
    displayName: 'Reddit',
    defaultScopes: ['identity'],
    iconName: 'heroChatBubbleLeftRight',
    scopesPlaceholder: 'identity, read, history',
  },
  {
    type: 'twitch',
    category: 'social',
    displayName: 'Twitch',
    defaultScopes: ['user:read:email'],
    iconName: 'heroVideoCamera',
    scopesPlaceholder: 'user:read:email, channel:read:subscriptions',
  },
  {
    type: 'spotify',
    category: 'social',
    displayName: 'Spotify',
    defaultScopes: ['user-read-email'],
    iconName: 'heroLink',
    scopesPlaceholder: 'user-read-email, playlist-read-private, user-library-read',
  },
  {
    type: 'dropbox',
    category: 'workspace',
    displayName: 'Dropbox',
    defaultScopes: ['account_info.read', 'files.metadata.read'],
    iconName: 'heroCloud',
    scopesPlaceholder: 'account_info.read, files.metadata.read, files.content.read',
  },
  {
    type: 'notion',
    category: 'workspace',
    displayName: 'Notion',
    defaultScopes: [],
    iconName: 'heroCloud',
    scopesPlaceholder: 'Usually left empty: Notion grants access per integration',
  },
  {
    type: 'hubspot',
    category: 'workspace',
    displayName: 'HubSpot',
    defaultScopes: ['oauth', 'crm.objects.contacts.read'],
    iconName: 'heroCloud',
    scopesPlaceholder: 'oauth, crm.objects.contacts.read, crm.objects.deals.read',
    hint: 'HubSpot requires the `oauth` scope.',
  },
  {
    type: 'okta',
    category: 'identity',
    displayName: 'Okta',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'auth0',
    category: 'identity',
    displayName: 'Auth0',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'cognito',
    category: 'identity',
    displayName: 'Amazon Cognito',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroCloud',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'cyberark',
    category: 'identity',
    displayName: 'CyberArk',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'fusionauth',
    category: 'identity',
    displayName: 'FusionAuth',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'onelogin',
    category: 'identity',
    displayName: 'OneLogin',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'ping',
    category: 'identity',
    displayName: 'PingOne',
    defaultScopes: ['openid', 'email', 'profile'],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile, offline_access',
  },
  {
    type: 'custom',
    category: 'custom',
    displayName: 'Custom (OIDC)',
    defaultScopes: [],
    iconName: 'heroLink',
    scopesPlaceholder: 'openid, email, profile',
    hint: 'Requires an OpenID Connect discovery URL',
  },
];

/** Human label for a provider type: the preset's name, 'Canvas LMS' for canvas. */
export function getConnectorTypeLabel(type: ConnectorType): string {
  if (type === 'canvas') return 'Canvas LMS';
  return getConnectorPreset(type)?.displayName ?? 'Custom';
}

export function getConnectorPreset(type: ConnectorType): ConnectorPreset | undefined {
  return CONNECTOR_PRESETS.find(preset => preset.type === type);
}

/**
 * True when the provider type needs an OIDC discovery URL.
 */
export function requiresDiscovery(type: ConnectorType): boolean {
  return type === 'custom' || type === 'canvas';
}

/**
 * Built-in vendors that live in the customer's own tenant. AgentCore ships
 * the vendor but needs the tenant's issuer and authorization/token endpoints,
 * which are sent as `authorizationServerMetadata`.
 */
export const TENANT_CONNECTOR_TYPES: readonly ConnectorType[] = [
  'okta',
  'auth0',
  'cognito',
  'cyberark',
  'fusionauth',
  'onelogin',
  'ping',
];

/** Issuer example shown per tenant vendor. */
export const TENANT_ISSUER_PLACEHOLDERS: Partial<Record<ConnectorType, string>> = {
  okta: 'https://your-tenant.okta.com/oauth2/your-authorization-server',
  auth0: 'https://your-tenant.auth0.com',
  cognito: 'https://cognito-idp.us-east-1.amazonaws.com/your-user-pool-id',
  cyberark: 'https://your-tenant-id.id.cyberark.cloud/__idaptive_cybr_user_oidc',
  fusionauth: 'https://your-tenant-token-issuer',
  onelogin: 'https://your-tenant.onelogin.com/oidc/2',
  ping: 'https://auth.pingone.com/your-env-id/as',
};

export function requiresTenantEndpoints(type: ConnectorType): boolean {
  return TENANT_CONNECTOR_TYPES.includes(type);
}
