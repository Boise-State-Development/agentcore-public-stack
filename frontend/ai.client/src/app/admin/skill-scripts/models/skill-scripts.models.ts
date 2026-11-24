/**
 * `/admin/skill-scripts` (skill-code-sandbox §8.10). Mirrors
 * `backend/src/apis/app_api/admin/skill_scripts/routes.py`, camelCase.
 */

export interface SkillScriptFile {
  /** Manifest path, e.g. `scripts/fill.py`. */
  path: string;
  contentHash: string;
  size: number;
  /** Read-only source, cut at 256 KiB; null when it could not be read. */
  source: string | null;
  truncated: boolean;
  unreadable: boolean;
}

export type ReviewScopeKind = 'agent' | 'project' | 'user';

export interface SkillScriptReview {
  scopeType: string;
  scopeId: string;
  scopeKind: ReviewScopeKind;
  scopeName: string | null;
  /** The Agent or project no longer exists (its review row outlived it). */
  scopeMissing: boolean;
  reviewedBy: string;
  reviewerName: string | null;
  reviewedAt: string | null;
  /** `review`, `update`, `authored` or `backfill`. */
  method: string;
  state: string;
  version: number | null;
}

export interface SkillScriptVersion {
  scriptsHash: string;
  isCurrent: boolean;
  switchedOff: boolean;
  /** `{manifest path: sha256}` for that version. */
  scripts: Record<string, string>;
  reviews: SkillScriptReview[];
}

export interface SkillScriptSwitch {
  off: boolean;
  /** `admin` or `author`; null when no switch row exists. */
  by: 'admin' | 'author' | null;
  byUserId: string | null;
  byName: string | null;
  at: string | null;
  reason: string | null;
  /** Only these versions are off; empty means every version. */
  hashes: string[];
}

export interface SkillScriptsDetail {
  skillId: string;
  displayName: string;
  status: string;
  ownerId: string;
  authorName: string | null;
  adminCatalog: boolean;
  provenance: Record<string, unknown> | null;
  scriptsHash: string | null;
  currentSwitchedOff: boolean;
  scripts: SkillScriptFile[];
  versions: SkillScriptVersion[];
  switch: SkillScriptSwitch;
}

export interface SkillScriptsSwitchBody {
  off: boolean;
  hashes?: string[];
  reason: string;
}
