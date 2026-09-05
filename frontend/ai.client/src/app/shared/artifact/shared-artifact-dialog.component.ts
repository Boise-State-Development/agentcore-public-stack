import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';

import { ArtifactViewerComponent } from '../../session/components/message-list/components/artifact/artifact-viewer.component';
import {
  ShareService,
  type SharedConversationArtifact,
} from '../../session/services/share/share.service';

export interface SharedArtifactDialogData {
  /** The CONVERSATION share this artifact came in on — the grant. */
  readonly shareId: string;
  readonly artifact: SharedConversationArtifact;
}

/**
 * Read-only viewer for one artifact inside a shared conversation.
 *
 * A dialog rather than the owner's docked panel, and deliberately not a
 * variant of it. `ArtifactPanelComponent` carries rename, delete, share,
 * a version picker, a code view and a download — every one of them an
 * endpoint keyed on something a recipient of a CONVERSATION share does
 * not have (an owned artifact, or an artifact share id). Bending it into a
 * "read-only mode" would mean a component whose every action is
 * conditional on a mode flag, which is how a missed condition becomes a
 * button that 403s.
 *
 * The body is `ArtifactViewerComponent`, which is purely presentational
 * and already serves the owner panel and the standalone recipient page
 * through two different mint endpoints. This is the third, and it needed
 * no change to that component — which is the sign the split was drawn in
 * the right place.
 *
 * Minting goes through the conversation share: there is no artifact
 * share record here, so the pair (shareId, artifactId) is the whole
 * handle, and the version served is the one the snapshot pinned.
 */
@Component({
  selector: 'app-shared-artifact-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ArtifactViewerComponent],
  providers: [provideIcons({ heroXMark })],
  host: { '(keydown.escape)': 'close()' },
  template: `
    <div
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      (click)="close()"
    >
      <div
        role="dialog"
        aria-modal="true"
        [attr.aria-label]="'Artifact: ' + (data.artifact.title || 'Untitled')"
        class="flex h-full max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800"
        (click)="$event.stopPropagation()"
      >
        <div
          class="flex shrink-0 items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700"
        >
          <div class="min-w-0 flex-1">
            <h2 class="truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
              {{ data.artifact.title || 'Untitled artifact' }}
            </h2>
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">
              Shared read-only
              @if (data.artifact.version > 1) {
                <span aria-hidden="true"> · </span>v{{ data.artifact.version }}
              }
            </p>
          </div>

          <button
            type="button"
            (click)="close()"
            class="grid size-8 shrink-0 place-items-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
            <span class="sr-only">Close</span>
          </button>
        </div>

        <div class="flex min-h-0 flex-1 flex-col">
          <!-- Preview only. The code view reads a content endpoint keyed
               on an artifact share id, and a conversation share does not
               have one — so the toggle is absent rather than present and
               permanently failing. Adding it is a backend change. -->
          <app-artifact-viewer
            [safeUrl]="safeUrl()"
            [title]="data.artifact.title"
            [error]="error()"
            [previewReady]="previewReady()"
            (iframeLoad)="onIframeLoad()"
            (retry)="retry()"
          />
        </div>
      </div>
    </div>
  `,
})
export class SharedArtifactDialogComponent {
  protected readonly data = inject<SharedArtifactDialogData>(DIALOG_DATA);
  private readonly dialogRef = inject<DialogRef<void>>(DialogRef);
  private readonly shares = inject(ShareService);
  private readonly sanitizer = inject(DomSanitizer);

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly error = signal<string | null>(null);

  private readonly iframeLoaded = signal(false);
  protected readonly previewReady = computed(
    () => !!this.safeUrl() && this.iframeLoaded(),
  );

  /** Bumped per mint so a slow response resolving after a retry is
   *  dropped rather than overwriting the newer one. */
  private requestSeq = 0;

  constructor() {
    void this.mint();
  }

  protected close(): void {
    this.dialogRef.close();
  }

  protected onIframeLoad(): void {
    this.iframeLoaded.set(true);
  }

  protected retry(): void {
    void this.mint();
  }

  private async mint(): Promise<void> {
    const seq = ++this.requestSeq;
    this.error.set(null);
    this.safeUrl.set(null);
    this.iframeLoaded.set(false);
    try {
      const token = await this.shares.mintConversationArtifactToken(
        this.data.shareId,
        this.data.artifact.artifactId,
      );
      if (seq !== this.requestSeq) {
        return;
      }
      this.safeUrl.set(
        this.sanitizer.bypassSecurityTrustResourceUrl(token.url),
      );
    } catch {
      if (seq !== this.requestSeq) {
        return;
      }
      // Deliberately one message for 404 and 403 alike. "Not part of
      // this share" and "you may not open this share" are the same fact
      // to a recipient, and distinguishing them would describe what the
      // owner has.
      this.error.set(
        "This artifact couldn't be loaded. It may have been removed, or " +
          'the conversation may no longer be shared with you.',
      );
    }
  }
}
