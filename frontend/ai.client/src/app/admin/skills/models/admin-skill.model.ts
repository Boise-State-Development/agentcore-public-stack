/**
 * Admin Skill catalog models — the TypeScript mirror of the backend
 * `apis/shared/skills/models.py` (`AdminSkillResponse`, `SkillResourceRef`,
 * the create/update/role DTOs). Shapes must stay in sync with that module
 * (CLAUDE.md cross-package contract).
 */

/**
 * Availability status of a skill (mirrors backend SkillStatus).
 */
export type SkillStatus = 'active' | 'draft' | 'disabled';

/**
 * Ownership visibility — reserved for Phase 2; v1 is always 'admin'.
 */
export type SkillVisibility = 'admin' | 'private' | 'shared';

/**
 * Manifest entry for one of a skill's supporting reference files. The bytes
 * live in S3 (the skill-resources bucket); this is the lightweight pointer
 * carried on the catalog row and returned by the /resources endpoints.
 */
export interface SkillResourceRef {
  filename: string;
  contentHash: string;
  size: number;
  contentType: string;
  s3Key: string;
}

/**
 * Admin skill definition with role assignments + reference-file manifest.
 */
export interface AdminSkill {
  skillId: string;
  displayName: string;
  description: string;
  instructions: string;
  boundToolIds: string[];
  compose: string[];
  resources: SkillResourceRef[];
  status: SkillStatus;
  category: string | null;
  ownerId: string;
  visibility: SkillVisibility;
  allowedAppRoles: string[];
  createdAt: string;
  updatedAt: string;
  createdBy: string | null;
  updatedBy: string | null;
}

/**
 * Response for listing admin skills.
 */
export interface AdminSkillListResponse {
  skills: AdminSkill[];
  total: number;
}

/**
 * Role assignment for a skill.
 */
export interface SkillRoleAssignment {
  roleId: string;
  displayName: string;
  grantType: 'direct' | 'inherited';
  inheritedFrom: string | null;
  enabled: boolean;
}

/**
 * Response for getting skill roles.
 */
export interface SkillRolesResponse {
  skillId: string;
  roles: SkillRoleAssignment[];
}

/**
 * Response for the /admin/skills/{id}/resources manifest endpoints.
 */
export interface SkillResourcesResponse {
  skillId: string;
  resources: SkillResourceRef[];
}

/**
 * Request body for POST /admin/skills.
 */
export interface SkillCreateRequest {
  skillId: string;
  displayName: string;
  description: string;
  instructions?: string;
  boundToolIds?: string[];
  compose?: string[];
  status?: SkillStatus;
  category?: string | null;
}

/**
 * Request body for PUT /admin/skills/{id}. All fields optional (partial update).
 */
export interface SkillUpdateRequest {
  displayName?: string;
  description?: string;
  instructions?: string;
  boundToolIds?: string[];
  compose?: string[];
  status?: SkillStatus;
  category?: string | null;
}

/**
 * Request body for setting/adding/removing skill role grants.
 */
export interface SetSkillRolesRequest {
  appRoleIds: string[];
}

/**
 * skill_id regex — identical to the backend SKILL_ID_PATTERN.
 */
export const SKILL_ID_PATTERN = /^[a-z][a-z0-9_]{2,49}$/;

/**
 * Available skill statuses for dropdowns.
 */
export const SKILL_STATUSES: { value: SkillStatus; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'draft', label: 'Draft' },
  { value: 'disabled', label: 'Disabled' },
];

/**
 * Suggested skill categories for the optional grouping dropdown. The backend
 * stores `category` as a free-form string, so this list is advisory.
 */
export const SKILL_CATEGORIES: { value: string; label: string }[] = [
  { value: 'document', label: 'Document' },
  { value: 'data', label: 'Data' },
  { value: 'research', label: 'Research' },
  { value: 'code', label: 'Code' },
  { value: 'productivity', label: 'Productivity' },
  { value: 'utility', label: 'Utility' },
  { value: 'custom', label: 'Custom' },
];
