/**
 * Skill version history — the wire types both tiers share
 * (docs/specs/skill-version-history.md §6). My Skills (`/skills/mine/{id}/versions*`)
 * and the admin catalog (`/admin/skills/{id}/versions*`) answer with the same
 * shapes, built by one backend module (`apis/app_api/skills/version_history_core.py`);
 * only who wrote a version and the restored skill's own shape differ.
 */

import { ImportFileRow, ImportTextDiff } from './skill-bundle-import.model';

/** What cut a version (`history.VersionTrigger`); null on rows cut before it was recorded. */
export type VersionTrigger =
  | 'create'
  | 'save'
  | 'file'
  | 'import'
  | 'reimport'
  | 'restore'
  | 'pin'
  | 'authoring'
  | 'checkpoint';

export interface SkillVersionSummary {
  version: number;
  createdAt?: string | null;
  trigger?: VersionTrigger | null;
  restoredFrom?: number | null;
  displayName: string;
  fileCount: number;
  /** Its content is the live skill's. */
  isCurrent: boolean;
  /** Provenance only: type, owner, repo, ref, commitSha. */
  importSource?: Record<string, string> | null;
  /** My Skills: whether you cut it. Never another person's name. */
  createdByYou?: boolean | null;
  /** Admin catalog: who cut it, when known. */
  createdByEmail?: string | null;
}

export interface SkillVersionList {
  versions: SkillVersionSummary[];
  /** Pass as `before` for the next page; absent at the end. */
  nextCursor?: number | null;
}

export interface SkillVersionFile {
  path: string;
  filename: string;
  kind: string;
  size: number;
}

/** One version, with the change from the live skill to it: what restoring would do. */
export interface SkillVersionDetail extends SkillVersionSummary {
  description: string;
  instructions: string;
  files: SkillVersionFile[];
  descriptionDiff: ImportTextDiff;
  instructionsDiff: ImportTextDiff;
  fileChanges: ImportFileRow[];
}

export interface SkillRestoreResult<TSkill> {
  skill: TSkill;
  /** The restore's own version; absent when keeping it failed (the restore still landed). */
  version?: SkillVersionSummary | null;
  /** False when the version predates full import sources, so the live one was kept. */
  importSourceRestored: boolean;
}

/**
 * What `SkillVersionHistoryComponent` needs from a tier. `MySkillService` and
 * `AdminSkillService` both implement it.
 */
export interface SkillVersionHistoryApi<TSkill> {
  listVersions(
    skillId: string,
    options?: { limit?: number; before?: number | null },
  ): Promise<SkillVersionList>;
  getVersion(skillId: string, version: number): Promise<SkillVersionDetail>;
  /** `confirmReach` only means anything to My Skills. */
  restoreVersion(
    skillId: string,
    version: number,
    options: { expectedUpdatedAt?: string | null; confirmReach?: boolean },
  ): Promise<SkillRestoreResult<TSkill>>;
  /** The version as an agentskills.io bundle (`…/export?version=n`). */
  exportSkill(skillId: string, version?: number): Promise<Blob>;
  describeError(err: unknown, fallback: string): string;
}

const TRIGGER_LABELS: Record<VersionTrigger, string> = {
  create: 'Created',
  save: 'Saved',
  file: 'Files changed',
  import: 'Imported',
  reimport: 'Re-imported',
  restore: 'Restored',
  pin: 'Pinned by a project or agent',
  authoring: 'Changed by an agent',
  checkpoint: 'Saved before a change',
};

/** What cut a version, in words. Rows from before triggers were recorded were all pins. */
export function versionTriggerLabel(row: SkillVersionSummary): string {
  if (row.trigger === 'restore' && row.restoredFrom) {
    return `Restored from version ${row.restoredFrom}`;
  }
  return TRIGGER_LABELS[row.trigger ?? 'pin'];
}

/** Who cut it: "You" / "Someone else" on My Skills, an email on the catalog. */
export function versionAuthorLabel(row: SkillVersionSummary): string | null {
  if (row.createdByYou === true) return 'You';
  if (row.createdByYou === false) return 'Someone else';
  return row.createdByEmail ?? null;
}

/** Consecutive file-by-file changes within this long collapse into one row. */
export const GROUP_WINDOW_MS = 10 * 60 * 1000;

const GROUPABLE: ReadonlySet<string> = new Set(['file', 'authoring']);

export interface VersionGroup {
  /** The newest version's number; stable while the list only grows. */
  key: number;
  rows: SkillVersionSummary[];
}

/**
 * Collapse runs of file changes (spec §6.2): consecutive rows (newest first)
 * cut by a file write or an authoring tool, by the same author, each within
 * ten minutes of the one before. Storage keeps every version; this is display.
 */
export function groupVersions(rows: SkillVersionSummary[]): VersionGroup[] {
  const groups: VersionGroup[] = [];
  for (const row of rows) {
    const last = groups[groups.length - 1];
    const prev = last?.rows[last.rows.length - 1];
    if (last && prev && sameRun(prev, row)) {
      last.rows.push(row);
    } else {
      groups.push({ key: row.version, rows: [row] });
    }
  }
  return groups;
}

function sameRun(newer: SkillVersionSummary, older: SkillVersionSummary): boolean {
  if (!GROUPABLE.has(newer.trigger ?? '') || newer.trigger !== older.trigger) return false;
  if (versionAuthorLabel(newer) !== versionAuthorLabel(older)) return false;
  const a = Date.parse(newer.createdAt ?? '');
  const b = Date.parse(older.createdAt ?? '');
  return !Number.isNaN(a) && !Number.isNaN(b) && a - b >= 0 && a - b <= GROUP_WINDOW_MS;
}
