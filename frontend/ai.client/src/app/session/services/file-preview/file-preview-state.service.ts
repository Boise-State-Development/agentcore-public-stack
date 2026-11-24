import { Injectable, computed, inject, signal } from '@angular/core';
import { DockedPaneService } from '../docked-pane/docked-pane.service';
import type { FilePreviewRef } from './file-preview.model';

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

  private readonly openRef = signal<FilePreviewRef | null>(null);
  private readonly hosts = signal(0);

  /**
   * Whether a preview pane is on the page to open files into. The pane
   * registers itself (`registerHost`), so a surface that renders citations
   * somewhere without one — the workflow designer's test run — can leave
   * its page links as plain text rather than offer links that do nothing.
   */
  readonly hostAvailable = computed(() => this.hosts() > 0);

  /** The file the pane is showing, or null when closed. */
  readonly openFile = computed<FilePreviewRef | null>(() =>
    this.dockedPane.owner() === 'file-preview' ? this.openRef() : null,
  );

  /** Called by a mounted preview pane; returns its unregister. */
  registerHost(): () => void {
    this.hosts.update((n) => n + 1);
    let registered = true;
    return () => {
      if (!registered) return;
      registered = false;
      this.hosts.update((n) => Math.max(0, n - 1));
    };
  }

  open(ref: FilePreviewRef): void {
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
