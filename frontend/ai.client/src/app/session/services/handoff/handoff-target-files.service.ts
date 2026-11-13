import { Injectable, inject, signal } from '@angular/core';

import { FileUploadService } from '../../../services/file-upload/file-upload.service';
import { DeliveryFile } from './handoff.models';

/**
 * The files a handoff's target made, for the handoff row in the source (handoffs spec §4.9).
 *
 * Read from the target's own file listing, which is the user's like any other, so the user can
 * watch the shared work grow and bring a file back with one click. Display only, like the row's
 * live status line: nothing here reaches the source's model until the user sends a card. Files
 * shared *into* the target (reference rows) are left out; they came from somewhere else.
 */
@Injectable({ providedIn: 'root' })
export class HandoffTargetFilesService {
  private readonly fileUpload = inject(FileUploadService);

  private readonly byTarget = signal<ReadonlyMap<string, readonly DeliveryFile[]>>(new Map());
  private readonly inFlight = new Set<string>();

  /** The target's own ready files, newest first; empty until read. */
  filesFor(targetSessionId: string | undefined): readonly DeliveryFile[] {
    return (targetSessionId && this.byTarget().get(targetSessionId)) || [];
  }

  /** Read the target's files again. One request at a time per target; a failure keeps the last list. */
  async refresh(targetSessionId: string): Promise<void> {
    if (!targetSessionId || this.inFlight.has(targetSessionId)) return;
    this.inFlight.add(targetSessionId);
    try {
      const listed = await this.fileUpload.listSessionFiles(targetSessionId);
      const own = listed
        .filter((f) => f.status === 'ready' && !f.linkedFrom)
        .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
        .map((f) => ({
          uploadId: f.uploadId,
          sessionId: targetSessionId,
          filename: f.filename,
          mimeType: f.mimeType,
          sizeBytes: f.sizeBytes,
        }));
      this.byTarget.update((map) => new Map(map).set(targetSessionId, own));
    } catch {
      // Keep what was shown; the next state change asks again.
    } finally {
      this.inFlight.delete(targetSessionId);
    }
  }
}
