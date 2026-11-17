/**
 * Front-end models for assistant KB sync policies — scheduled re-index of
 * knowledge sources (imported Drive files, web crawls).
 *
 * Mirrors the backend contract from `apis/app_api/sync_policies/` — field
 * names are the camelCase aliases the backend serializes.
 */

/**
 * `drive_folder` keeps a whole Drive folder in step (assistant-kb-sync spec §11):
 * its `sourceRef` is `{connectorId}:{folderId}`, and it carries the folder's
 * path (`sourceName`) and its last run's counts (`lastRunSummary`).
 */
export type SyncSourceType = 'drive_file' | 'web_crawl' | 'drive_folder';
export type SyncInterval = 'daily' | 'weekly' | 'monthly';
export type SyncPolicyState =
  | 'active'
  | 'paused_error'
  | 'paused_inactive'
  | 'paused_reauth'
  | 'paused_user';
export type SyncRunResult = 'changed' | 'unchanged' | 'failed' | 'skipped';

/** What a `drive_folder` run did, as counts (backend FolderRunSummary). */
export interface FolderRunSummary {
  listed: number;
  unchanged: number;
  updated: number;
  added: number;
  removedFromSource: number;
  failed: number;
  skipped: number;
  deferred: number;
  truncated: boolean;
}

/** Public view of a sync policy (backend SyncPolicyResponse). */
export interface SyncPolicy {
  policyId: string;
  assistantId: string;
  sourceType: SyncSourceType;
  sourceRef: string;
  /** drive_folder: the folder's path. */
  sourceName?: string | null;
  interval: SyncInterval;
  state: SyncPolicyState;
  stateReason?: string | null;
  nextSyncAt?: string | null;
  lastSyncAt?: string | null;
  lastResult?: SyncRunResult | null;
  /** drive_folder: the last run's counts. */
  lastRunSummary?: FolderRunSummary | null;
  createdAt: string;
  updatedAt: string;
}

/** Request body for POST /assistants/{id}/sync-policies. */
export interface CreateSyncPolicyRequest {
  sourceType: SyncSourceType;
  sourceRef: string;
  interval: SyncInterval;
  /** drive_folder only: the folder's path, shown beside the schedule. */
  sourceName?: string;
}

/**
 * Request body for PATCH /assistants/{id}/sync-policies/{policyId}.
 * `state` accepts only the user-owned transitions: 'paused_user' (pause)
 * and 'active' (resume). Resuming a paused_reauth policy is rejected with
 * 409 — only a fresh OAuth consent resumes those.
 */
export interface UpdateSyncPolicyRequest {
  interval?: SyncInterval;
  state?: 'active' | 'paused_user';
}

/** Response from GET /assistants/{id}/sync-policies. */
export interface SyncPoliciesListResponse {
  policies: SyncPolicy[];
}

/**
 * A folder sync's last run, in words: how many files the folder holds and what
 * changed (assistant-kb-sync spec §11). `null` before the first run.
 */
export function folderRunSummaryText(summary: FolderRunSummary | null | undefined): string | null {
  if (!summary) return null;
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  const parts = [
    summary.updated ? `${summary.updated} updated` : '',
    summary.added ? `${summary.added} added` : '',
    summary.removedFromSource ? `${summary.removedFromSource} no longer in the folder` : '',
    summary.failed ? `${plural(summary.failed, 'file', 'files')} couldn’t be fetched` : '',
    summary.skipped ? `${plural(summary.skipped, 'file', 'files')} too large` : '',
    summary.deferred ? `${summary.deferred} waiting for the next sync` : '',
  ].filter(Boolean);
  const listed = `${plural(summary.listed, 'file', 'files')} in the folder${summary.truncated ? ' (more than are checked)' : ''}`;
  return parts.length ? `${listed} · last sync: ${parts.join(', ')}` : `${listed} · up to date at the last sync`;
}

/** The connector half of a `drive_folder` policy's `{connectorId}:{folderId}`. */
export function folderConnectorId(sourceRef: string): string | null {
  const at = sourceRef.lastIndexOf(':');
  return at > 0 ? sourceRef.slice(0, at) : null;
}
