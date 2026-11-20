import { HttpErrorResponse } from '@angular/common/http';

/**
 * Where an imported skill came from, as the read APIs carry it
 * (`SkillImportSource.provenance()` on the backend): no URL, path or bundle hash.
 */
export interface SkillProvenance {
  type: 'zip' | 'github' | string;
  owner?: string | null;
  repo?: string | null;
  ref?: string | null;
  commitSha?: string | null;
}

/**
 * "Imported from owner/repo @ abc1234", "Imported from a zip file", or null for a
 * hand-written skill. Plain text: callers interpolate it, never bind it as HTML,
 * because owner and repo are whatever the third party named them.
 */
export function provenanceLine(source: SkillProvenance | null | undefined): string | null {
  if (!source || typeof source !== 'object' || !source.type) return null;
  if (source.type === 'github' && source.owner && source.repo) {
    const sha = source.commitSha ? ` @ ${source.commitSha.slice(0, 7)}` : '';
    return `Imported from ${source.owner}/${source.repo}${sha}`;
  }
  if (source.type === 'zip') return 'Imported from a zip file';
  return 'Imported from a bundle';
}

/** A skill binding's pinned version, read from its `config` the way the backend stores it. */
export function bindingPinVersion(config: Record<string, unknown> | null | undefined): number | null {
  const value = config?.['version'];
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 ? value : null;
}

/** "Pinned to v3". */
export function pinnedLabel(version: number): string {
  return `Pinned to v${version}`;
}

/** The `detail.code` of the 409 a skill write returns until the reach is confirmed. */
export const REACH_CONFIRMATION_CODE = 'skill_reach_unconfirmed';

/**
 * The reach summary from a 409 that asks the caller to confirm an edit reaching
 * other people's agents or projects, or null for any other error.
 */
export function reachConfirmationFrom(err: unknown): string | null {
  if (!(err instanceof HttpErrorResponse) || err.status !== 409) return null;
  const detail = (err.error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const { code, reach } = detail as { code?: unknown; reach?: unknown };
  return code === REACH_CONFIRMATION_CODE && typeof reach === 'string' ? reach : null;
}
