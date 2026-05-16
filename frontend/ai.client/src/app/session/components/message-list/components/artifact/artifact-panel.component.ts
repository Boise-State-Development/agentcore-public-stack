import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroArrowPath,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { SessionService } from '../../../../services/session/session.service';

/**
 * Right slide-over that renders one artifact version in a sandboxed
 * iframe. Open state lives in `ArtifactStateService`; this component
 * mints a fresh short-lived render token each time it opens (the token
 * is a ~120s bearer credential — never cached) and points the iframe at
 * the returned artifact-origin URL.
 *
 * Isolation model (from the #306 design): the artifact origin is a
 * separate subdomain, the render Lambda + CloudFront stamp a strict CSP,
 * and the iframe carries `sandbox="allow-scripts"` *without*
 * `allow-same-origin` so the framed document is a null origin and cannot
 * reach the artifact origin's storage/cookies. `bypassSecurityTrustResourceUrl`
 * is safe here: the URL comes from our own authenticated app-api, is
 * single-use, and the sandbox + CSP contain the content.
 */
@Component({
  selector: 'app-artifact-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroXMark, heroArrowPath, heroExclamationTriangle }),
  ],
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    @if (open(); as ref) {
      <div
        class="fixed inset-0 z-40 bg-black/30"
        (click)="close()"
        aria-hidden="true"
      ></div>
      <aside
        class="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
        role="dialog"
        aria-modal="true"
        [attr.aria-label]="'Artifact: ' + ref.title"
      >
        <header
          class="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700"
        >
          <div class="min-w-0 flex-1">
            <h2
              class="truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            >
              {{ ref.title || 'Untitled artifact' }}
            </h2>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              Version {{ ref.version }}
            </p>
          </div>
          <button
            type="button"
            class="flex h-8 w-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            aria-label="Close artifact"
            (click)="close()"
          >
            <ng-icon name="heroXMark" class="text-lg" aria-hidden="true" />
          </button>
        </header>

        <div class="relative min-h-0 flex-1">
          @if (loading()) {
            <div
              class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-gray-500 dark:text-gray-400"
              role="status"
            >
              <ng-icon
                name="heroArrowPath"
                class="animate-spin text-2xl"
                aria-hidden="true"
              />
              <span class="text-sm">Loading artifact…</span>
            </div>
          } @else if (error()) {
            <div
              class="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center"
              role="alert"
            >
              <ng-icon
                name="heroExclamationTriangle"
                class="text-3xl text-amber-500"
                aria-hidden="true"
              />
              <p class="text-sm text-gray-700 dark:text-gray-300">
                {{ error() }}
              </p>
              <button
                type="button"
                class="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-900"
                (click)="retry()"
              >
                Try again
              </button>
            </div>
          } @else if (safeUrl()) {
            <iframe
              [src]="safeUrl()"
              class="h-full w-full border-0 bg-white"
              [title]="ref.title || 'Artifact'"
              sandbox="allow-scripts"
              referrerpolicy="no-referrer"
              loading="lazy"
            ></iframe>
          }
        </div>
      </aside>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class ArtifactPanelComponent {
  private artifactState = inject(ArtifactStateService);
  private artifactHttp = inject(ArtifactHttpService);
  private sessionService = inject(SessionService);
  private sanitizer = inject(DomSanitizer);

  protected readonly open = this.artifactState.openArtifact;

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<string | null>(null);

  /** Bumped per open/retry so a slow mint that resolves after the panel
   *  closed (or moved to another artifact) is discarded. */
  private requestSeq = 0;

  protected readonly currentKey = computed(() => {
    const r = this.open();
    return r ? `${r.artifactId}#${r.version}` : null;
  });

  constructor() {
    // Mint a fresh token whenever the panel opens or switches artifact.
    // Closing (open() === null) clears the iframe so the credential URL
    // doesn't linger in the DOM.
    effect(() => {
      const key = this.currentKey();
      if (!key) {
        this.reset();
        return;
      }
      void this.load();
    });
  }

  protected onEscape(): void {
    if (this.open()) this.close();
  }

  protected close(): void {
    this.artifactState.closeArtifactPanel();
  }

  protected retry(): void {
    void this.load();
  }

  private reset(): void {
    this.safeUrl.set(null);
    this.error.set(null);
    this.loading.set(false);
  }

  private async load(): Promise<void> {
    const ref = this.open();
    if (!ref) return;
    const seq = ++this.requestSeq;
    this.loading.set(true);
    this.error.set(null);
    this.safeUrl.set(null);
    try {
      const sessionId = this.sessionService.currentSession().sessionId;
      const token = await this.artifactHttp.mintRenderToken(
        ref.artifactId,
        ref.version,
        sessionId,
      );
      if (seq !== this.requestSeq) return; // superseded — drop
      this.safeUrl.set(
        this.sanitizer.bypassSecurityTrustResourceUrl(token.url),
      );
    } catch {
      if (seq !== this.requestSeq) return;
      this.error.set(
        "This artifact couldn't be loaded. It may have expired or been removed.",
      );
    } finally {
      if (seq === this.requestSeq) this.loading.set(false);
    }
  }
}
