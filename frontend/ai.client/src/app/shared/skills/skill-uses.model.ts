/**
 * Where a skill is used, and moving pins behind it
 * (docs/specs/skill-version-history.md D9, §6.3). The TypeScript mirror of
 * `apis/app_api/skills/uses_routes.py`.
 */

/** An Agent by its id, or a project by its project id. */
export type SkillUseKind = 'agent' | 'project';

export interface SkillUse {
  kind: SkillUseKind;
  id: string;
  name: string;
  pinnedVersion?: number | null;
  canUpdate: boolean;
  /** Runs for someone other than the skill's owner. */
  shared: boolean;
  /** A store Agent: an update lands on its draft until it is resubmitted. */
  published: boolean;
}

export interface SkillUses {
  /** Unpinned, or pinned to the live content: these already run the current skill. */
  current: SkillUse[];
  /** Pinned to an older version, in a place the owner can update. */
  behind: SkillUse[];
  /** Pinned behind where the owner can't update: counted, never named. */
  othersBehind: number;
  /** False when the lookup failed or stopped early. */
  complete: boolean;
}

export interface SkillUseTarget {
  kind: SkillUseKind;
  id: string;
}

export interface SkillUseUpdateResult extends SkillUseTarget {
  ok: boolean;
  version?: number | null;
  error?: string | null;
}

/** What `SkillUsesComponent` needs. My Skills only: the catalog enumerates nothing (D9). */
export interface SkillUsesApi {
  getUses(skillId: string): Promise<SkillUses>;
  updateUses(skillId: string, targets: SkillUseTarget[]): Promise<SkillUseUpdateResult[]>;
  describeError(err: unknown, fallback: string): string;
}

export function useKey(use: SkillUseTarget): string {
  return `${use.kind}:${use.id}`;
}
