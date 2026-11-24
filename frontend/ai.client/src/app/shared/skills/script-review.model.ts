import { HttpErrorResponse } from '@angular/common/http';
import { SkillProvenance, provenanceLine } from './skill-provenance';

/**
 * Who may run a skill's scripts, from the binder's side (docs/specs/skill-code-sandbox.md
 * §8.5): the 409 a save answers while a skill it binds has scripts nobody reviewed for
 * this Agent or project, and each binding's state for its row note.
 */

/** The `detail.code` of the 409 an Agent or project save answers until the scripts are reviewed. */
export const SCRIPTS_UNREVIEWED_CODE = 'skill_scripts_unreviewed';

/** How many hex characters of a script set's hash a person is shown ("version 3f2a9c1e"). */
const SHORT_HASH = 8;

export type ScriptChange = 'new' | 'changed' | 'removed' | 'same';

export interface ScriptFileRow {
  /** The bundle path, e.g. `scripts/fill_fillable_fields.py`. */
  path: string;
  size: number;
  change: ScriptChange;
}

export interface SkillFileRow {
  path: string;
  size: number;
}

/** One skill in the 409 body: what the review sheet shows. */
export interface UnreviewedScriptSkill {
  skillId: string;
  displayName: string;
  scriptsHash: string;
  /** The version the binding would pin. */
  version: number;
  /** The version last reviewed for this Agent or project, when this is an Update. */
  fromVersion: number | null;
  /** When that version was reviewed. */
  reviewedAt: string | null;
  provenance: SkillProvenance | null;
  /** The author's display name. */
  author: string | null;
  scripts: ScriptFileRow[];
  /** The skill's other files, which a script may read. */
  files: SkillFileRow[];
}

/** The fields a save carries once the sheet is answered. */
export interface ScriptReviewAnswers {
  /** `{skillId: scriptsHash}` the binder allowed. */
  scriptsReviewed?: Record<string, string>;
  /** Skills bound without allowing their scripts ("Add without scripts"). */
  scriptsDeclined?: string[];
}

/** A bound skill's script state for this Agent or project. */
export type SkillScriptState =
  | 'allowed'
  | 'authored'
  | 'admin'
  | 'not_allowed'
  | 'off_by_author'
  | 'off_by_admin'
  | 'unreviewable';

/** One row of `GET /agents/{id}/skill-scripts` or `GET /projects/{id}/skill-scripts`. */
export interface SkillScriptStatus {
  skillId: string;
  displayName: string;
  version: number | null;
  scriptsHash: string | null;
  state: SkillScriptState;
  /** `review`, `authored`, `update` or `backfill`, for an allowed state. */
  method: string | null;
  author: string | null;
  reviewer: string | null;
  reviewedAt: string | null;
  provenance: SkillProvenance | null;
  /** Editors only: the live skill's scripts differ from the pinned version's. */
  scriptsChanged?: boolean;
  /** Editors only. */
  scripts?: ScriptFileRow[];
  files?: SkillFileRow[];
}

export interface SkillScriptsResponse {
  skills: SkillScriptStatus[];
}

/** "3f2a9c1e". */
export function scriptsVersion(hash: string | null | undefined): string {
  return (hash ?? '').slice(0, SHORT_HASH);
}

/** The skills from a `skill_scripts_unreviewed` 409, or null for any other error. */
export function unreviewedScriptsFrom(err: unknown): UnreviewedScriptSkill[] | null {
  if (!(err instanceof HttpErrorResponse) || err.status !== 409) return null;
  const detail = (err.error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const { code, skills } = detail as { code?: unknown; skills?: unknown };
  if (code !== SCRIPTS_UNREVIEWED_CODE || !Array.isArray(skills) || !skills.length) return null;
  return skills.filter(isUnreviewedSkill);
}

function isUnreviewedSkill(value: unknown): value is UnreviewedScriptSkill {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return typeof v['skillId'] === 'string' && typeof v['scriptsHash'] === 'string' && Array.isArray(v['scripts']);
}

/** The sheet's input for a binding row's "Review" (an existing binding, not a 409). */
export function reviewFromStatus(status: SkillScriptStatus): UnreviewedScriptSkill | null {
  if (!status.scriptsHash || status.version === null) return null;
  return {
    skillId: status.skillId,
    displayName: status.displayName,
    scriptsHash: status.scriptsHash,
    version: status.version,
    fromVersion: null,
    reviewedAt: null,
    provenance: status.provenance,
    author: status.author,
    scripts: status.scripts ?? [],
    files: status.files ?? [],
  };
}

/** "Written by Pat", "Imported from owner/repo @ abc1234 by Pat", "Imported from a zip file by Pat". */
export function scriptsProvenanceLine(skill: Pick<UnreviewedScriptSkill, 'provenance' | 'author'>): string {
  const imported = provenanceLine(skill.provenance);
  const by = skill.author ? ` by ${skill.author}` : '';
  if (imported) return `${imported}${by}`;
  return skill.author ? `Written by ${skill.author}` : 'Written by its author';
}

/** "4 KB", "812 bytes". */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? 'byte' : 'bytes'}`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** "Nov 23, 2026". */
export function reviewDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/** What a binding row says about its skill's scripts, and whether it offers Review. */
export interface BindingScriptsNote {
  text: string;
  tone: 'muted' | 'warning';
  /** The row offers "Review" (the sheet on an existing binding). */
  review: boolean;
}

/**
 * The binding row's line (§8.5, "The binding row, afterwards"). `scope` names who the
 * scripts run for: "everyone who uses {Agent}" or "everyone in {project}".
 */
export function bindingScriptsNote(status: SkillScriptStatus, scope: string): BindingScriptsNote | null {
  const version = scriptsVersion(status.scriptsHash);
  switch (status.state) {
    case 'authored':
      return { text: `Scripts run for ${scope} · version ${version}`, tone: 'muted', review: false };
    case 'allowed': {
      if (status.scriptsChanged && status.provenance) {
        return { text: 'Scripts changed since your review', tone: 'warning', review: false };
      }
      const date = reviewDate(status.reviewedAt);
      return { text: `Scripts allowed · version ${version}${date ? ` · ${date}` : ''}`, tone: 'muted', review: false };
    }
    case 'not_allowed':
      return { text: 'Scripts not allowed', tone: 'warning', review: true };
    case 'off_by_admin':
      return { text: 'Scripts turned off by an administrator', tone: 'warning', review: false };
    case 'off_by_author':
      return { text: "Scripts turned off by the skill's author", tone: 'warning', review: false };
    case 'unreviewable':
      return { text: "This skill's scripts need to be saved again before they can run for others", tone: 'warning', review: false };
    case 'admin':
      return null;
  }
}
