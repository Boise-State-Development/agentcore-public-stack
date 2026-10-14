import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';

/**
 * A Google Drive folder attached to a conversation
 * (docs/specs/drive-folder-attachments.md). One attachment on the message,
 * whatever its file count: app-api expands it into session files in the
 * background and the turn carries a manifest of them, never their bytes.
 */

/** `expanding` while app-api walks the folder; the other three are settled. */
export type FolderStatus = 'expanding' | 'ready' | 'partial' | 'failed';

export interface FolderCounts {
  listed: number;
  attached: number;
  skippedType: number;
  skippedSize: number;
  failed: number;
  truncated: boolean;
}

export interface FolderFileEntry {
  providerFileId: string;
  name: string;
  relativePath: string;
  mimeType: string;
  sizeBytes: number | null;
  uploadId: string | null;
  status: 'pending' | 'attached' | 'failed' | 'skipped_size' | 'removed';
  error: string | null;
  /** Provider version stamp and content hash when attached (refresh's change gates). */
  etag?: string | null;
  contentHash?: string | null;
  /** Still attached and readable, but gone from the folder at the last refresh. */
  removedFromSource?: boolean;
}

/** What the last refresh did (spec §4.6). */
export interface RefreshSummary {
  unchanged: number;
  updated: number;
  added: number;
  removedFromSource: number;
  failed: number;
}

/** The folder row, as `GET /sessions/{id}/folders/{folderAttachId}` returns it. */
export interface FolderAttachment {
  folderAttachId: string;
  sessionId: string;
  connectorId: string;
  connectorName: string | null;
  providerFolderId: string;
  folderName: string;
  folderPath: string | null;
  status: FolderStatus;
  /** `needs_consent` | `listing_failed` | `quota_exceeded`, when one applies. */
  error: string | null;
  counts: FolderCounts;
  files: FolderFileEntry[];
  snapshotAt: string | null;
  /** True while a refresh (not the first expansion) is walking the folder. */
  refreshing?: boolean;
  refreshedAt?: string | null;
  lastRefresh?: RefreshSummary | null;
  createdAt: string;
  updatedAt: string;
  /** Expanding, but untouched past the server's timeout: the task is gone and needs `resume`. */
  stale: boolean;
}

/**
 * A source folder the user attached before, as `GET /folders/recent` returns it:
 * one per folder, newest first, with what a new attach needs and nothing of the
 * conversation it came from (docs/specs/composer-project-folder-picker.md §3.3).
 */
export interface RecentFolder {
  connectorId: string;
  connectorName: string | null;
  providerFolderId: string;
  folderName: string;
  folderPath: string | null;
  lastAttachedAt: string;
}

/** How many recent folders the picker asks for. The server allows at most 20. */
export const RECENT_FOLDERS_LIMIT = 8;

/** Body of `POST /sessions/{id}/folders`. */
export interface AttachFolderRequest {
  connectorId: string;
  folderId: string;
  folderName: string;
  folderPath?: string | null;
}

/** An API failure with its HTTP status, so callers can tell a gone row (404) from the rest. */
export class FolderAttachmentError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'FolderAttachmentError';
  }
}

/** Folders one message may carry. Must match `FOLDER_ATTACH_MAX_PER_MESSAGE` on the Runtime. */
export const MAX_FOLDERS_PER_MESSAGE = 2;

/**
 * Milliseconds between polls of an expanding folder, the last repeated.
 *
 * Quicker at the start than the attachments KB's cadence (10–30 s): a folder
 * of a few dozen small files expands in seconds on dev, and the card's count
 * is the only progress the user sees, so the first answers should come soon.
 */
export const FOLDER_POLL_DELAYS_MS: readonly number[] = [2_000, 3_000, 5_000, 10_000, 15_000, 20_000, 30_000];

/** Stop polling a folder after this long; it reads as stale and resumes on the next visit. */
export const FOLDER_POLL_MAX_MS = 20 * 60 * 1000;

export function isSettledStatus(status: FolderStatus): boolean {
  return status !== 'expanding';
}

/** The files the model can read, in the walk's order. */
export function attachedFolderFiles(row: FolderAttachment): FolderFileEntry[] {
  return row.files.filter((file) => file.status === 'attached' && !!file.uploadId);
}

/** Files the walk could not attach, for the card's tooltip and the count. */
export function skippedFolderCount(row: FolderAttachment): number {
  return row.counts.skippedType + row.counts.skippedSize + row.counts.failed;
}

/**
 * The card's one line of status: "Adding 3 of 6…", "6 files", "4 of 6 files · 2 skipped",
 * "Couldn't read this folder".
 */
export function folderStatusLabel(row: FolderAttachment): string {
  const attached = row.counts.attached;
  const listed = row.counts.listed;
  if (row.status === 'expanding') {
    if (row.refreshing) return 'Checking for changes…';
    return listed > 0 ? `Adding ${attached} of ${listed}…` : 'Reading folder…';
  }
  if (row.status === 'failed' && attached === 0) {
    return row.error === 'needs_consent' ? 'Reconnect to read this folder' : "Couldn't read this folder";
  }
  const files = `${attached} ${attached === 1 ? 'file' : 'files'}`;
  const skipped = skippedFolderCount(row);
  let label: string;
  if (row.status === 'ready' || skipped === 0) {
    label = row.counts.truncated ? `${files} (limit reached)` : files;
  } else {
    label = `${attached} of ${listed} files · ${skipped} skipped`;
  }
  const refreshed = refreshSummaryLabel(row);
  return refreshed ? `${label} · ${refreshed}` : label;
}

/** What the last refresh changed: "1 updated, 2 new", or "up to date". Empty before any refresh. */
export function refreshSummaryLabel(row: FolderAttachment): string {
  const summary = row.lastRefresh;
  if (!summary || !row.refreshedAt) return '';
  const parts: string[] = [];
  if (summary.updated) parts.push(`${summary.updated} updated`);
  if (summary.added) parts.push(`${summary.added} new`);
  if (summary.removedFromSource) parts.push(`${summary.removedFromSource} gone from the folder`);
  return parts.length > 0 ? parts.join(', ') : 'up to date';
}

/** Why files were left out, one clause each, for the card's tooltip. Empty when none were. */
export function folderSkipReasons(row: FolderAttachment): string {
  const reasons: string[] = [];
  const c = row.counts;
  if (c.skippedType) reasons.push(`${c.skippedType} not a type the assistant can read (images, other formats)`);
  if (c.skippedSize) reasons.push(`${c.skippedSize} too large`);
  if (c.failed) reasons.push(`${c.failed} could not be downloaded`);
  if (c.truncated) reasons.push('the folder has more files than one attachment takes');
  if (row.error === 'quota_exceeded') reasons.push('your file storage is full');
  return reasons.join('; ');
}

/** `POST/GET/DELETE /sessions/{id}/folders…` on app-api. Owner-scoped server-side. */
@Injectable({ providedIn: 'root' })
export class FolderAttachmentService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/sessions`);

  private url(sessionId: string, folderAttachId?: string): string {
    const base = `${this.baseUrl()}/${encodeURIComponent(sessionId)}/folders`;
    return folderAttachId ? `${base}/${encodeURIComponent(folderAttachId)}` : base;
  }

  /** Attach a folder and start expanding it. Returns the row at once (`expanding`). */
  async attach(sessionId: string, request: AttachFolderRequest): Promise<FolderAttachment> {
    try {
      return await firstValueFrom(this.http.post<FolderAttachment>(this.url(sessionId), request));
    } catch (err) {
      throw this.toError(err, 'Could not attach the folder.');
    }
  }

  async get(sessionId: string, folderAttachId: string): Promise<FolderAttachment> {
    try {
      return await firstValueFrom(this.http.get<FolderAttachment>(this.url(sessionId, folderAttachId)));
    } catch (err) {
      throw this.toError(err, 'Could not read the folder.');
    }
  }

  /** Remove the folder and every file it attached. A row already gone counts as removed. */
  async remove(sessionId: string, folderAttachId: string): Promise<void> {
    try {
      await firstValueFrom(this.http.delete<void>(this.url(sessionId, folderAttachId)));
    } catch (err) {
      const error = this.toError(err, 'Could not remove the folder.');
      if (error.status !== 404) throw error;
    }
  }

  /** This user's folders in a conversation, newest first. */
  async list(sessionId: string): Promise<FolderAttachment[]> {
    try {
      const body = await firstValueFrom(this.http.get<{ folders: FolderAttachment[] }>(this.url(sessionId)));
      return body.folders;
    } catch (err) {
      throw this.toError(err, 'Could not list the folders.');
    }
  }

  /** The source folders the user attached most recently, one per folder, newest first. */
  async listRecent(limit = RECENT_FOLDERS_LIMIT): Promise<RecentFolder[]> {
    try {
      const body = await firstValueFrom(
        this.http.get<{ folders: RecentFolder[] }>(`${this.config.appApiUrl()}/folders/recent`, {
          params: { limit },
        }),
      );
      return body.folders;
    } catch (err) {
      throw this.toError(err, 'Could not list your recent folders.');
    }
  }

  /** Re-list the folder and bring its files up to date (spec §4.6). Returns the row refreshing. */
  async refresh(sessionId: string, folderAttachId: string): Promise<FolderAttachment> {
    try {
      return await firstValueFrom(
        this.http.post<FolderAttachment>(`${this.url(sessionId, folderAttachId)}/refresh`, {}),
      );
    } catch (err) {
      throw this.toError(err, 'Could not refresh the folder.');
    }
  }

  /** Re-run a walk whose task was lost, skipping files already attached. */
  async resume(sessionId: string, folderAttachId: string): Promise<FolderAttachment> {
    try {
      return await firstValueFrom(
        this.http.post<FolderAttachment>(`${this.url(sessionId, folderAttachId)}/resume`, {}),
      );
    } catch (err) {
      throw this.toError(err, 'Could not resume the folder.');
    }
  }

  private toError(err: unknown, fallback: string): FolderAttachmentError {
    if (err instanceof HttpErrorResponse) {
      const detail = typeof err.error?.detail === 'string' ? err.error.detail : null;
      if (err.status === 409) {
        return new FolderAttachmentError(detail ?? 'Connect Google Drive to attach this folder.', 409);
      }
      return new FolderAttachmentError(detail ?? fallback, err.status);
    }
    return new FolderAttachmentError(fallback, 0);
  }
}
