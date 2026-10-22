import { Injectable, computed, inject, signal } from '@angular/core';
import { DockedPaneService } from '../docked-pane/docked-pane.service';
import type { OpenFilePreviewRef } from './file-preview.model';

/**
 * Open-pane state for the docked file preview.
 *
 * Deliberately thin next to `ArtifactStateService`: there is no registry
 * to keep. Artifacts are a per-session collection the conversation
 * renders cards for, so that service holds every version it has seen.
 * A previewed file is opened straight from a card that already carries
 * its `upload_id` and `filename`, and everything else the pane needs is
 * fetched on demand — so the only state worth holding is *which file*,
 * plus the rail claim.
 *
 * Shares the one right-docked rail with the artifact pane via
 * `DockedPaneService`; opening a preview evicts an open artifact and
 * vice versa. `openFile` is gated on the rail's owner for the same
 * reason `ArtifactStateService.openArtifact` is — eviction is a read,
 * not a callback.
 */
@Injectable({ providedIn: 'root' })
export class FilePreviewStateService {
  private readonly dockedPane = inject(DockedPaneService);

  private readonly openRef = signal<OpenFilePreviewRef | null>(null);

  /** The file the pane is showing, or null when closed. */
  readonly openFile = computed<OpenFilePreviewRef | null>(() =>
    this.dockedPane.owner() === 'file-preview' ? this.openRef() : null,
  );

  open(ref: OpenFilePreviewRef): void {
    this.openRef.set(ref);
    this.dockedPane.claim('file-preview');
  }

  close(): void {
    this.openRef.set(null);
    this.dockedPane.release('file-preview');
  }

  /** Clear state — called on session change, alongside the artifact
   *  registry's own reset. A preview left open across a session switch
   *  would show a file that belongs to the conversation the user just
   *  left. */
  reset(): void {
    this.close();
  }
}
