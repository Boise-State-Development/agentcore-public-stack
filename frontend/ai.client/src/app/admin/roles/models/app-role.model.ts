/**
 * Effective permissions computed from role + inheritance.
 */
export interface EffectivePermissions {
  /** Tool IDs accessible via this role */
  tools: string[];
  /** Model IDs accessible via this role */
  models: string[];
  /** Admin areas granted by this role (never inherited) */
  adminScopes: string[];
  /** Quota tier ID, if assigned */
  quotaTier: string | null;
}

/**
 * Application Role definition.
 */
export interface AppRole {
  /** Unique role identifier (lowercase alphanumeric + underscore) */
  roleId: string;
  /** Human-readable display name */
  displayName: string;
  /** Admin-facing description */
  description: string;
  /** JWT roles that grant this AppRole */
  jwtRoleMappings: string[];
  /** Parent AppRole IDs to inherit from */
  inheritsFrom: string[];
  /** Directly granted tool IDs */
  grantedTools: string[];
  /** Directly granted model IDs */
  grantedModels: string[];
  /**
   * Directly granted admin scopes. Unlike the three axes above these are never
   * inherited from `inheritsFrom`, and there is no `'*'` wildcard — full admin
   * is the `system_admin` role, not a scope.
   */
  grantedAdminScopes: string[];
  /** Pre-computed effective permissions (from grants + inheritance) */
  effectivePermissions: EffectivePermissions;
  /** Priority for quota tier selection (0-999, higher wins) */
  priority: number;
  /** Whether this is a protected system role */
  isSystemRole: boolean;
  /** Whether this role is active */
  enabled: boolean;
  /** ISO 8601 creation timestamp */
  createdAt: string;
  /** ISO 8601 update timestamp */
  updatedAt: string;
  /** User ID who created the role */
  createdBy: string;
  /**
   * Free-text instruction added to the system prompt of plain chats (no agent)
   * for every user holding this role. Not inherited. `null` or absent: none.
   */
  systemInstruction?: string | null;
}

/**
 * Response model for listing AppRoles.
 */
export interface AppRoleListResponse {
  roles: AppRole[];
  total: number;
}

/**
 * Request model for creating a new AppRole.
 */
export interface AppRoleCreateRequest {
  /** Unique role identifier (lowercase alphanumeric + underscore, 3-50 chars) */
  roleId: string;
  /** Human-readable display name (1-100 chars) */
  displayName: string;
  /** Admin-facing description (0-500 chars) */
  description?: string;
  /** JWT roles that grant this AppRole */
  jwtRoleMappings?: string[];
  /** Parent AppRole IDs to inherit from */
  inheritsFrom?: string[];
  /** Directly granted tool IDs */
  grantedTools?: string[];
  /** Directly granted model IDs */
  grantedModels?: string[];
  /** Delegated admin areas. Non-delegable ids are rejected by the server. */
  grantedAdminScopes?: string[];
  /** Priority for quota tier selection (0-999) */
  priority?: number;
  /** Whether this role is active */
  enabled?: boolean;
  /** System instruction (max 8,000 chars). Empty or `null` clears it. */
  systemInstruction?: string | null;
}

/**
 * Request model for updating an AppRole.
 * All fields are optional for partial updates.
 */
export interface AppRoleUpdateRequest {
  /** Human-readable display name (1-100 chars) */
  displayName?: string;
  /** Admin-facing description (0-500 chars) */
  description?: string;
  /** JWT roles that grant this AppRole */
  jwtRoleMappings?: string[];
  /** Parent AppRole IDs to inherit from */
  inheritsFrom?: string[];
  /** Directly granted tool IDs */
  grantedTools?: string[];
  /** Directly granted model IDs */
  grantedModels?: string[];
  /** Delegated admin areas. Non-delegable ids are rejected by the server. */
  grantedAdminScopes?: string[];
  /** Priority for quota tier selection (0-999) */
  priority?: number;
  /** Whether this role is active */
  enabled?: boolean;
  /** System instruction (max 8,000 chars). Empty or `null` clears it. */
  systemInstruction?: string | null;
}

/**
 * Form data model for creating/editing an AppRole.
 */
export interface AppRoleFormData {
  roleId: string;
  displayName: string;
  description: string;
  jwtRoleMappings: string[];
  inheritsFrom: string[];
  grantedTools: string[];
  grantedModels: string[];
  grantedAdminScopes: string[];
  priority: number;
  enabled: boolean;
  systemInstruction: string;
}

/** Per-role cap on `systemInstruction`; the server enforces the same limit. */
export const MAX_ROLE_SYSTEM_INSTRUCTION_CHARS = 8000;
