import { Injectable, inject, signal } from '@angular/core';
import { AttachmentSearchStatus, FileUploadService } from './file-upload.service';

/** What a large document's chip says. */
export type LargeDocumentChip = 'read-on-demand' | 'indexing' | 'searchable';

/**
 * Chip copy, shared by the composer card (short form, with the full form as its
 * tooltip and accessible text) and the message card (full form).
 */
export const LARGE_DOCUMENT_CHIP_COPY: Readonly<Record<LargeDocumentChip, { short: string; full: string }>> = {
  'read-on-demand': { short: 'Read on demand', full: 'Large document, read on demand' },
  indexing: { short: 'Indexing…', full: 'Large document, indexing for search…' },
  searchable: { short: 'Searchable', full: 'Large document, searchable' },
};

/** Statuses that are still moving, so the chip polls again. */
const MOVING: ReadonlySet<AttachmentSearchStatus> = new Set(['preparing', 'indexing', 'evicted']);

/**
 * Seconds between polls. Ingest into a managed KB took 68–264 s for small
 * files on dev (spec §9 D3), so the first answer comes after a few polls and
 * later ones space out.
 */
export const SEARCH_STATUS_POLL_DELAYS_MS: readonly number[] = [10_000, 15_000, 20_000, 30_000];

/**
 * Stop polling after this long. A document still indexing then is left alone
 * until the conversation is next rendered; the daily reconcile settles it
 * server-side either way.
 */
export const SEARCH_STATUS_POLL_LIMIT_MS = 30 * 60_000;

/**
 * The chip for a status. Anything not searchable yet but on its way says so;
 * everything else (no knowledge base, over the cap, failed) is still read on
 * demand, which always works.
 */
export function largeDocumentChip(status: AttachmentSearchStatus | null): LargeDocumentChip {
  if (status === 'searchable') return 'searchable';
  if (status !== null && MOVING.has(status)) return 'indexing';
  return 'read-on-demand';
}

/**
 * The search status of large attachments, one poll per upload however many
 * cards show it (the composer card and the message card are the same file).
 *
 * Only injected while `features.attachmentKb` is on. A card calls `track()`
 * when it shows a ready reference-class document and reads `statusFor()` in a
 * `computed()`. Polling stops at a settled status, on an error (a deleted file
 * 404s), or after `SEARCH_STATUS_POLL_LIMIT_MS`; an upload that stopped for an
 * error or the time limit can be tracked again later.
 */
@Injectable({ providedIn: 'root' })
export class AttachmentSearchStatusService {
  private readonly fileUpload = inject(FileUploadService);
  private readonly statuses = signal<ReadonlyMap<string, AttachmentSearchStatus>>(new Map());
  private readonly tracked = new Set<string>();

  statusFor(uploadId: string): AttachmentSearchStatus | null {
    return this.statuses().get(uploadId) ?? null;
  }

  track(uploadId: string): void {
    if (!uploadId || this.tracked.has(uploadId)) return;
    const known = this.statuses().get(uploadId);
    if (known && !MOVING.has(known)) return;
    this.tracked.add(uploadId);
    void this.poll(uploadId, 0, Date.now());
  }

  private async poll(uploadId: string, attempt: number, startedAt: number): Promise<void> {
    let status: AttachmentSearchStatus;
    try {
      status = (await this.fileUpload.getSearchStatus(uploadId)).status;
    } catch {
      this.tracked.delete(uploadId);
      return;
    }
    this.statuses.update((current) => new Map(current).set(uploadId, status));

    if (!MOVING.has(status)) return;
    if (Date.now() - startedAt >= SEARCH_STATUS_POLL_LIMIT_MS) {
      this.tracked.delete(uploadId);
      return;
    }
    const delays = SEARCH_STATUS_POLL_DELAYS_MS;
    const delay = delays[Math.min(attempt, delays.length - 1)];
    setTimeout(() => void this.poll(uploadId, attempt + 1, startedAt), delay);
  }
}
