import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { Document, STALE_DOCUMENT_THRESHOLD_MS } from '../models/document.model';
import { DocumentUploadError } from './document.service';
import { ToastService } from '../../services/toast/toast.service';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog';

/** The document pipeline's per-file limit, the same one both upload screens enforce. */
export const MAX_REPLACE_FILE_BYTES = 10 * 1024 * 1024;

/**
 * How long a replacement is watched before giving up. The backend auto-fails a
 * document stuck processing for {@link STALE_DOCUMENT_THRESHOLD_MS}, so polling a
 * minute past that ends on a terminal status rather than on the clock.
 */
export const REPLACE_POLL_LIMIT_MS = STALE_DOCUMENT_THRESHOLD_MS + 60_000;

/** A file name's extension, lowercased with its dot (`'.pdf'`), or `''` when it has none. */
export function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 && dot < name.length - 1 ? name.slice(dot).toLowerCase() : '';
}

/** Whether two file names share an extension, ignoring case. */
export function hasSameExtension(originalName: string, pickedName: string): boolean {
  return fileExtension(originalName) === fileExtension(pickedName);
}

/**
 * The managed knowledge base's byte cap refusing an upload, in either shape it
 * reaches a caller: {@link DocumentUploadError} from `DocumentService`, or the raw
 * `HttpErrorResponse` the project routes pass through.
 */
export function isByteCapError(err: unknown): boolean {
  return (
    (err instanceof DocumentUploadError && err.code === 'HTTP_413') ||
    (err instanceof HttpErrorResponse && err.status === 413)
  );
}

/**
 * Reorder a document list so each replacement sits directly after the document it
 * was meant to replace. A link whose original is no longer listed leaves the
 * replacement where the server put it.
 */
export function orderReplacementsAdjacent<T extends { documentId: string }>(
  docs: readonly T[],
  links: ReadonlyMap<string, string>,
): T[] {
  if (links.size === 0) return [...docs];
  const present = new Set(docs.map((d) => d.documentId));
  const moved = new Set(
    [...links].filter(([newId, oldId]) => present.has(newId) && present.has(oldId)).map(([newId]) => newId),
  );
  if (moved.size === 0) return [...docs];
  const byId = new Map(docs.map((d) => [d.documentId, d]));
  const result: T[] = [];
  for (const doc of docs) {
    if (moved.has(doc.documentId)) continue;
    result.push(doc);
    for (const [newId, oldId] of links) {
      if (oldId === doc.documentId && moved.has(newId)) result.push(byId.get(newId)!);
    }
  }
  return result;
}

/** Why a picked file can't replace a document, or `null` when it can. */
export function validateReplacement(oldDoc: Pick<Document, 'filename'>, file: File): string | null {
  if (!hasSameExtension(oldDoc.filename, file.name)) {
    const ext = fileExtension(oldDoc.filename);
    const wanted = ext ? `a ${ext} file` : 'a file with no extension';
    return `Choose ${wanted} — a replacement must be the same type as “${oldDoc.filename}”.`;
  }
  if (file.size > MAX_REPLACE_FILE_BYTES) {
    return 'Files can be up to 10 MB.';
  }
  return null;
}

/**
 * How a screen talks to its document routes. The agent knowledge base and a
 * project's files reach the same pipeline through different routes, so each
 * supplies its own; the replace sequence itself is shared.
 */
export interface DocumentReplaceTransport {
  /** Create the new document and return where to PUT its bytes. */
  requestUploadUrl(file: File): Promise<{ documentId: string; uploadUrl: string }>;
  /** PUT the bytes. The screen shows the progress. */
  uploadToS3(uploadUrl: string, file: File): Promise<void>;
  /** Mark a document whose bytes never arrived as failed (best effort). */
  reportUploadFailure(documentId: string, message: string, details?: string): Promise<void>;
  /** Reload the screen's list. */
  refresh(): Promise<void>;
  /** Show the new row and poll it; `null` when the wait ended without a terminal status. */
  waitForTerminal(documentId: string): Promise<Document | null>;
  /** Delete a document. Throws on failure and never toasts. */
  deleteDocument(documentId: string): Promise<void>;
}

export interface ReplaceOutcome {
  kind: 'replaced' | 'failed' | 'cancelled' | 'rejected' | 'blocked' | 'interrupted';
  newDocumentId?: string;
  message?: string;
}

/**
 * Replace a knowledge document with a newer version, upload first: the new file is
 * uploaded and processed while the old one keeps serving, and the old one is
 * deleted only once the new one is ready. The replacement takes the original's
 * file name, so the knowledge base ends up with the same name and type.
 *
 * One replace runs at a time, app-wide. While it runs, a persistent notice asks the
 * user to keep the tab open and the browser warns before unload. If they leave
 * anyway, both versions remain.
 */
@Injectable({ providedIn: 'root' })
export class DocumentReplaceService {
  private readonly toast = inject(ToastService);
  private readonly backgroundTasks = inject(BackgroundTaskService);
  private readonly dialog = inject(Dialog);

  private readonly active = signal<{ oldId: string; newId: string | null } | null>(null);
  private readonly links = signal<ReadonlyMap<string, string>>(new Map());

  /** New document id → the id of the document it was meant to replace. */
  readonly replacementLinks = this.links.asReadonly();
  readonly inFlight = computed(() => this.active() !== null);
  readonly activeOldId = computed(() => this.active()?.oldId ?? null);

  private readonly beforeUnload = (event: BeforeUnloadEvent): void => {
    event.preventDefault();
    event.returnValue = '';
  };

  /** Whether a document is either side of the replace in flight. */
  isBusy(documentId: string): boolean {
    const active = this.active();
    return !!active && (active.oldId === documentId || active.newId === documentId);
  }

  /** The document a replacement was meant to replace, when it is one. */
  replacementFor(newDocumentId: string): string | null {
    return this.links().get(newDocumentId) ?? null;
  }

  /** Drop any link touching a document, e.g. once the user deletes it. */
  forget(documentId: string): void {
    const current = this.links();
    if (![...current].some(([n, o]) => n === documentId || o === documentId)) return;
    this.links.set(new Map([...current].filter(([n, o]) => n !== documentId && o !== documentId)));
  }

  async replace(
    oldDoc: Document,
    picked: File,
    transport: DocumentReplaceTransport,
  ): Promise<ReplaceOutcome> {
    if (this.inFlight()) return { kind: 'blocked' };

    const name = oldDoc.filename;
    const invalid = validateReplacement(oldDoc, picked);
    if (invalid) {
      this.toast.error(`Can’t replace “${name}”`, invalid);
      return { kind: 'rejected', message: invalid };
    }

    // Same bytes under the original's name: the backend stores the name it is
    // given and derives the S3 key from it, so the new version keeps the name.
    const file = new File([picked], name, { type: picked.type, lastModified: picked.lastModified });
    const oldId = oldDoc.documentId;
    this.active.set({ oldId, newId: null });
    const taskId = this.backgroundTasks.start(
      `Replacing “${name}”`,
      'Keep this tab open until the new version is ready.',
    );
    window.addEventListener('beforeunload', this.beforeUnload);

    try {
      let deletedFirst = false;
      let started: { documentId: string; uploadUrl: string };
      try {
        started = await transport.requestUploadUrl(file);
      } catch (err) {
        if (!isByteCapError(err)) throw err;
        if (!(await this.confirmDeleteFirst(name, err))) {
          return { kind: 'cancelled' };
        }
        await transport.deleteDocument(oldId);
        this.forget(oldId);
        deletedFirst = true;
        started = await transport.requestUploadUrl(file);
      }

      const newId = started.documentId;
      if (!deletedFirst) {
        this.links.update((m) => new Map(m).set(newId, oldId));
      }
      this.active.set({ oldId, newId });

      try {
        await transport.uploadToS3(started.uploadUrl, file);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        const details = err instanceof DocumentUploadError ? JSON.stringify(err.details ?? {}) : undefined;
        await transport.reportUploadFailure(newId, message, details);
        await transport.refresh();
        this.toast.error(`“${name}” couldn’t be replaced`, this.failureDetail(deletedFirst, message));
        return { kind: 'failed', newDocumentId: newId, message };
      }

      const final = await transport.waitForTerminal(newId);
      if (final?.status === 'complete') {
        if (!deletedFirst) {
          try {
            await transport.deleteDocument(oldId);
          } catch {
            this.toast.error(
              `New version is ready, but the old “${name}” couldn’t be removed`,
              'Delete it manually.',
            );
          }
        }
        this.forget(newId);
        this.toast.success(`“${name}” replaced`);
        return { kind: 'replaced', newDocumentId: newId };
      }
      if (final?.status === 'failed') {
        this.toast.error(`“${name}” couldn’t be replaced`, this.failureDetail(deletedFirst, final.errorMessage));
        return { kind: 'failed', newDocumentId: newId, message: final.errorMessage ?? undefined };
      }
      this.toast.warning(
        `“${name}” is still processing`,
        'Both versions are kept for now — delete the older one once the new one is ready.',
      );
      return { kind: 'interrupted', newDocumentId: newId };
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Something went wrong.';
      this.toast.error(`“${name}” couldn’t be replaced`, message);
      return { kind: 'failed', message };
    } finally {
      this.active.set(null);
      this.backgroundTasks.dismiss(taskId);
      window.removeEventListener('beforeunload', this.beforeUnload);
    }
  }

  private failureDetail(deletedFirst: boolean, reason?: string | null): string {
    if (deletedFirst) {
      return 'The new version failed to process, and the previous version was already removed.';
    }
    return `The new version failed to process${reason ? `: ${reason}` : ''}. The current version is still in use.`;
  }

  /** The byte cap has no room for both versions: offer to remove the current one first. */
  private async confirmDeleteFirst(name: string, err: unknown): Promise<boolean> {
    const detail =
      err instanceof HttpErrorResponse
        ? (err.error?.detail ?? err.message)
        : err instanceof Error
          ? err.message
          : '';
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: {
        title: 'Not enough room for both versions',
        message:
          `${detail ? `${detail} ` : ''}Replace anyway? The current “${name}” will be removed first, ` +
          'and it can’t be restored if the new version fails.',
        confirmText: 'Remove current and replace',
        destructive: true,
      },
    });
    return (await firstValueFrom(ref.closed)) === true;
  }
}
