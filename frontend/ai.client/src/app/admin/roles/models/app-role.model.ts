/**
 * Effective permissions computed from role + inheritance.
 */
export interface EffectivePermissions {
  /** Tool IDs accessible via this role */
  tools: string[];
  /** Model IDs accessible via this role */
  models: string[];
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
}

/**
 * Response model for listing AppRoles.
 */
export interface AppRoleListResponse {
  roles: AppRole[];
  total: number;
}
