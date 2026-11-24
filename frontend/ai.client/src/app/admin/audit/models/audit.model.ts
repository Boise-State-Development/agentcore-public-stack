/**
 * The administrative audit trail — client mirror of `apis/shared/audit`.
 *
 * `before` and `after` carry only the fields a mutation actually changed, so a
 * record is a diff rather than a pair of snapshots. `changes` names those fields
 * and is what the list row renders; the expanded row reads the two maps.
 */

/** One recorded administrative mutation. */
export interface AuditRecord {
  auditId: string;
  /** ISO 8601. May carry the legacy `+00:00Z` spelling on older rows. */
  timestamp: string;
  /** e.g. `app_role.updated`. Closed set, served by `GET /admin/audit/actions`. */
  action: string;
  actorUserId: string;
  actorEmail: string;
  targetType: string;
  targetId: string;
  outcome: 'allowed' | 'denied';
  /** Field names this mutation touched. */
  changes: string[];
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  /** Present only on a denied attempt — why the guard refused. */
  reason?: string;
}

export interface AuditPage {
  records: AuditRecord[];
  /** Opaque. Absent when the current month has no further pages. */
  nextCursor: string | null;
  /** Which month the server actually read, including when none was requested. */
  month?: string;
}

export interface AuditActionsResponse {
  actions: string[];
}

/**
 * Human labels for the closed action set.
 *
 * Falls back to the raw id, so a new action added server-side shows up as
 * `app_role.something` rather than vanishing from the list.
 */
const ACTION_LABELS: Record<string, string> = {
  'app_role.created': 'Role created',
  'app_role.updated': 'Role updated',
  'app_role.deleted': 'Role deleted',
  'app_role.synced': 'Permissions synced',
  'app_role.mutation_denied': 'Change denied',
  'skill.created': 'Skill created',
  'skill.updated': 'Skill updated',
  'skill.imported': 'Skill imported',
  'skill.refreshed': 'Skill re-imported',
  'skill.restored': 'Skill version restored',
  'skill.deleted': 'Skill deleted',
  'skill.exported': 'Skill exported',
  'skill.script_run': 'Skill script run',
  'skill.scripts_reviewed': 'Skill scripts allowed',
  'skill.scripts_review_withdrawn': 'Skill scripts no longer allowed',
  'skill.scripts_switched': 'Skill scripts switched on or off',
  'sandbox.code_run': 'Sandbox code run',
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

/** Field names as they read in the role form, so the diff matches the UI. */
const FIELD_LABELS: Record<string, string> = {
  display_name: 'Display name',
  description: 'Description',
  jwt_role_mappings: 'JWT role mappings',
  inherits_from: 'Inherits from',
  granted_tools: 'Tools',
  granted_models: 'Models',
  granted_skills: 'Skills',
  granted_admin_scopes: 'Admin access',
  priority: 'Priority',
  enabled: 'Enabled',
  // Skill records (ids and hashes only).
  contentHash: 'Content hash',
  files: 'Files',
  importSource: 'Imported from',
  ownerId: 'Owner',
  tier: 'Tier',
  scriptPath: 'Script',
  scriptSha256: 'Script SHA-256',
  outcome: 'Outcome',
};

export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field;
}

/** Render a before/after value for display without leaning on JSON braces. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '(none)';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (value === '') return '(empty)';
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length ? entries.map(([k, v]) => `${k}: ${formatValue(v)}`).join(', ') : '(none)';
  }
  return String(value);
}
