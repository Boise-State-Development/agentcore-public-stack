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
 * Right-docked pane that renders one artifact version in a sandboxed
 * iframe. Non-modal: the side nav collapses to free the space (handled
 * by `ArtifactStateService`) and the chat stays visible and interactive
 * alongside it. Open state lives in `ArtifactStateService`; this component
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
      <aside
        class="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900"
        [style.maxWidth]="paneWidthCss()"
        [class.select-none]="dragging()"
        [attr.aria-label]="'Artifact: ' + ref.title"
      >
        <div
          role="separator"
          aria-orientation="vertical"
          tabindex="0"
          aria-label="Resize artifact panel"
          [attr.aria-valuemin]="paneWidthMin"
          [attr.aria-valuemax]="paneWidthMax"
          [attr.aria-valuenow]="paneWidth()"
          class="group absolute inset-y-0 left-0 z-10 flex w-2 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center focus-visible:outline-none"
          (pointerdown)="onHandlePointerDown($event)"
          (pointermove)="onHandlePointerMove($event)"
          (pointerup)="onHandlePointerUp($event)"
          (pointercancel)="onHandlePointerUp($event)"
          (keydown)="onHandleKeydown($event)"
        >
          <span
            aria-hidden="true"
            class="h-12 w-1 rounded-full bg-gray-300 transition-colors group-hover:bg-blue-500 group-focus-visible:bg-blue-500 dark:bg-gray-600 dark:group-hover:bg-blue-400"
          ></span>
        </div>
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
              [class.pointer-events-none]="dragging()"
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

  // Resize handle state. Width itself lives in ArtifactStateService so
  // the layout (content padding + fixed footer/topnav) can reserve the
  // same amount via a CSS var.
  protected readonly paneWidth = this.artifactState.paneWidth;
  protected readonly paneWidthMin = this.artifactState.paneWidthMin;
  protected readonly paneWidthMax = this.artifactState.paneWidthMax;
  protected readonly paneWidthCss = computed(
    () => `${this.artifactState.paneWidth()}px`,
  );
  protected readonly dragging = signal(false);
  private dragStartX = 0;
  private dragStartWidth = 0;

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

  protected onHandlePointerDown(e: PointerEvent): void {
    e.preventDefault();
    // Capture so the drag keeps tracking even while the cursor is over
    // the sandboxed iframe (which would otherwise swallow the events).
    try {
      (e.target as Element).setPointerCapture(e.pointerId);
    } catch {
      /* not all pointer types/environments allow capture — drag still works */
    }
    this.dragStartX = e.clientX;
    this.dragStartWidth = this.artifactState.paneWidth();
    this.dragging.set(true);
  }

  protected onHandlePointerMove(e: PointerEvent): void {
    if (!this.dragging()) return;
    // Pane is docked to the right edge: dragging the handle left (a
    // smaller clientX) widens it.
    const delta = this.dragStartX - e.clientX;
    this.applyWidth(this.dragStartWidth + delta);
  }

  protected onHandlePointerUp(e: PointerEvent): void {
    if (!this.dragging()) return;
    this.dragging.set(false);
    try {
      const el = e.target as Element;
      if (el.hasPointerCapture(e.pointerId)) {
        el.releasePointerCapture(e.pointerId);
      }
    } catch {
      /* capture may never have been established — nothing to release */
    }
  }

  protected onHandleKeydown(e: KeyboardEvent): void {
    const step = e.shiftKey ? 64 : 16;
    const w = this.artifactState.paneWidth();
    switch (e.key) {
      case 'ArrowLeft': // widen (boundary moves left)
        this.applyWidth(w + step);
        break;
      case 'ArrowRight': // narrow
        this.applyWidth(w - step);
        break;
      case 'Home':
        this.applyWidth(this.paneWidthMax);
        break;
      case 'End':
        this.applyWidth(this.paneWidthMin);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  /** Clamp to the absolute service bounds and to a viewport-relative
   *  ceiling so the chat column can never be squeezed away entirely. */
  private applyWidth(px: number): void {
    let target = px;
    if (typeof window !== 'undefined') {
      const maxByViewport = Math.max(
        this.paneWidthMin,
        window.innerWidth - 360,
      );
      target = Math.min(target, maxByViewport);
    }
    this.artifactState.setPaneWidth(target);
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
