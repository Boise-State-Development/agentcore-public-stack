import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { HttpErrorResponse } from '@angular/common/http';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroArrowPath,
  heroExclamationTriangle,
  heroArrowDownTray,
  heroEye,
  heroCodeBracket,
  heroClipboard,
  heroCheck,
  heroChevronUpDown,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import {
  ArtifactHttpService,
  type ArtifactContent,
} from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { SessionService } from '../../../../services/session/session.service';
import { ArtifactSourceComponent } from './artifact-source.component';

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
  imports: [NgIcon, ArtifactSourceComponent],
  providers: [
    provideIcons({
      heroXMark,
      heroArrowPath,
      heroExclamationTriangle,
      heroArrowDownTray,
      heroEye,
      heroCodeBracket,
      heroClipboard,
      heroCheck,
      heroChevronUpDown,
    }),
  ],
  host: {
    '(document:keydown.escape)': 'onEscape()',
    '(document:pointerdown)': 'onDocumentPointerDown($event)',
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
            @if (versions().length > 1) {
              <div #versionControl class="relative">
                <button
                  type="button"
                  class="-ml-1 flex items-center gap-0.5 rounded px-1 py-0.5 text-xs text-gray-500 transition-colors hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-400 dark:hover:text-gray-100"
                  aria-haspopup="menu"
                  [attr.aria-expanded]="menuOpen()"
                  aria-controls="artifact-version-menu"
                  [attr.aria-label]="
                    'Version ' +
                    ref.version +
                    ' of ' +
                    versions().length +
                    ', change version'
                  "
                  (click)="toggleMenu()"
                >
                  <span>Version {{ ref.version }}</span>
                  <ng-icon
                    name="heroChevronUpDown"
                    class="text-sm"
                    aria-hidden="true"
                  />
                </button>
                @if (menuOpen()) {
                  <div
                    id="artifact-version-menu"
                    role="menu"
                    aria-label="Artifact versions"
                    class="absolute left-0 z-20 mt-1 max-h-72 w-60 overflow-auto rounded-lg border border-gray-200 bg-white p-1 shadow-lg dark:border-gray-700 dark:bg-gray-800"
                    (keydown)="onMenuKeydown($event)"
                  >
                    @for (v of versions(); track v.version) {
                      <button
                        type="button"
                        role="menuitemradio"
                        [attr.aria-checked]="v.version === ref.version"
                        class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500"
                        [class]="
                          v.version === ref.version
                            ? 'bg-gray-100 text-gray-900 dark:bg-gray-700 dark:text-gray-100'
                            : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
                        "
                        (click)="selectVersion(v)"
                      >
                        <span
                          class="flex h-4 w-4 shrink-0 items-center justify-center"
                          aria-hidden="true"
                        >
                          @if (v.version === ref.version) {
                            <ng-icon name="heroCheck" class="text-sm" />
                          }
                        </span>
                        <span class="min-w-0 flex-1">
                          <span class="font-medium">Version {{ v.version }}</span>
                          @if (relativeLabel(v.updatedAt); as t) {
                            <span class="ml-1 text-gray-400 dark:text-gray-500"
                              >· {{ t }}</span
                            >
                          }
                        </span>
                        @if (v.version === latestVersion()) {
                          <span
                            class="shrink-0 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-blue-700 dark:bg-blue-500/15 dark:text-blue-300"
                            >Latest</span
                          >
                        }
                      </button>
                    }
                  </div>
                }
              </div>
            } @else {
              <p class="text-xs text-gray-500 dark:text-gray-400">
                Version {{ ref.version }}
              </p>
            }
          </div>
          <div
            role="group"
            aria-label="Artifact view mode"
            class="flex items-center gap-0.5 rounded-md border border-gray-200 p-0.5 dark:border-gray-700"
          >
            <button
              type="button"
              class="flex h-7 w-7 items-center justify-center rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              [class]="
                view() === 'preview'
                  ? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100'
                  : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              "
              [attr.aria-pressed]="view() === 'preview'"
              aria-label="Preview"
              title="Preview"
              (click)="setView('preview')"
            >
              <ng-icon name="heroEye" class="text-base" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="flex h-7 w-7 items-center justify-center rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              [class]="
                view() === 'code'
                  ? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100'
                  : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              "
              [attr.aria-pressed]="view() === 'code'"
              aria-label="View code"
              title="View code"
              (click)="setView('code')"
            >
              <ng-icon
                name="heroCodeBracket"
                class="text-base"
                aria-hidden="true"
              />
            </button>
          </div>
          @if (view() === 'code') {
            <button
              type="button"
              class="flex h-8 w-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
              [attr.aria-label]="copied() ? 'Copied' : 'Copy code'"
              [disabled]="!source()"
              (click)="copy()"
            >
              <ng-icon
                [name]="copied() ? 'heroCheck' : 'heroClipboard'"
                class="text-lg"
                [class.text-green-600]="copied()"
                aria-hidden="true"
              />
            </button>
          }
          <button
            type="button"
            class="flex h-8 w-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            [attr.aria-label]="
              downloading() ? 'Downloading artifact…' : 'Download artifact'
            "
            [attr.aria-busy]="downloading()"
            [disabled]="downloading() || !safeUrl()"
            (click)="download()"
          >
            <ng-icon
              [name]="downloading() ? 'heroArrowPath' : 'heroArrowDownTray'"
              class="text-lg"
              [class.animate-spin]="downloading()"
              aria-hidden="true"
            />
          </button>
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
          @if (view() === 'code') {
            @if (sourceLoading()) {
              <div
                class="absolute inset-0 flex flex-col items-center justify-center gap-2 text-gray-500 dark:text-gray-400"
                role="status"
              >
                <ng-icon
                  name="heroArrowPath"
                  class="animate-spin text-2xl"
                  aria-hidden="true"
                />
                <span class="text-sm">Loading source…</span>
              </div>
            } @else if (sourceError()) {
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
                  {{ sourceError() }}
                </p>
                <button
                  type="button"
                  class="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-900"
                  (click)="retrySource()"
                >
                  Try again
                </button>
              </div>
            } @else if (source(); as src) {
              <app-artifact-source
                [content]="src.content"
                [contentType]="src.contentType"
              />
            }
          } @else {
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
  private artifactDownload = inject(ArtifactDownloadService);

  protected readonly open = this.artifactState.openArtifact;

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly downloading = signal(false);

  // Code-view state. The source is fetched lazily the first time the
  // user switches to 'code' for a given artifact (the preview iframe
  // path is untouched and stays the default).
  protected readonly view = signal<'preview' | 'code'>('preview');
  protected readonly source = signal<ArtifactContent | null>(null);
  protected readonly sourceLoading = signal(false);
  protected readonly sourceError = signal<string | null>(null);
  protected readonly copied = signal(false);
  /** Bumped per source fetch so a slow response that resolves after the
   *  panel closed or switched artifact is discarded. */
  private sourceSeq = 0;
  /** Which `currentKey()` the loaded source belongs to, so re-opening
   *  the same version doesn't refetch but switching does. */
  private loadedSourceKey: string | null = null;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

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

  // Version picker. All versions live in ArtifactStateService (one
  // registry entry per `artifactId#version`); selecting one just
  // re-points openRef and the currentKey effect reloads it.
  protected readonly versions = computed<Artifact[]>(() => {
    const r = this.open();
    return r ? this.artifactState.versionsFor(r.artifactId) : [];
  });
  /** Highest version number (the list is desc) — drives the badge. */
  protected readonly latestVersion = computed(
    () => this.versions()[0]?.version,
  );
  protected readonly menuOpen = signal(false);
  private readonly versionControl =
    viewChild<ElementRef<HTMLElement>>('versionControl');

  constructor() {
    // Mint a fresh token whenever the panel opens or switches artifact.
    // Closing (open() === null) clears the iframe so the credential URL
    // doesn't linger in the DOM.
    effect(() => {
      const key = this.currentKey();
      // Any open / version-switch / close also dismisses the menu.
      this.menuOpen.set(false);
      if (!key) {
        this.reset();
        return;
      }
      // New artifact/version: drop any stale source and return to the
      // preview default so the toggle doesn't carry across artifacts.
      this.resetSource();
      this.view.set('preview');
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
    if (this.menuOpen()) {
      this.closeMenuAndRefocus();
      return;
    }
    if (this.open()) this.close();
  }

  protected toggleMenu(): void {
    const next = !this.menuOpen();
    this.menuOpen.set(next);
    // Let the menu render, then move focus into it for keyboard users.
    if (next) setTimeout(() => this.focusSelectedItem());
  }

  protected selectVersion(v: Artifact): void {
    this.menuOpen.set(false);
    // Re-points openRef; the currentKey effect re-mints the token and
    // reloads (preview) / refetches (code) for the chosen version, and
    // the header title tracks that version.
    this.artifactState.openArtifactPanel({
      artifactId: v.artifactId,
      version: v.version,
      title: v.title,
    });
  }

  protected onMenuKeydown(e: KeyboardEvent): void {
    const items = this.menuItems();
    if (!items.length) return;
    const idx = items.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        items[(Math.max(idx, -1) + 1) % items.length]?.focus();
        break;
      case 'ArrowUp':
        e.preventDefault();
        items[(idx <= 0 ? items.length : idx) - 1]?.focus();
        break;
      case 'Home':
        e.preventDefault();
        items[0]?.focus();
        break;
      case 'End':
        e.preventDefault();
        items[items.length - 1]?.focus();
        break;
      case 'Tab':
        // Don't leave an orphaned menu when focus tabs away.
        this.menuOpen.set(false);
        break;
      default:
        break;
    }
  }

  protected onDocumentPointerDown(e: PointerEvent): void {
    if (!this.menuOpen()) return;
    const root = this.versionControl()?.nativeElement;
    if (root && !root.contains(e.target as Node)) {
      this.menuOpen.set(false);
    }
  }

  /** Short relative time for a version row; '' (hidden) if unparseable. */
  protected relativeLabel(iso: string): string {
    if (!iso) return '';
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return '';
    const min = Math.floor((Date.now() - then) / 60000);
    if (min < 1) return 'just now';
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  }

  private menuItems(): HTMLButtonElement[] {
    const root = this.versionControl()?.nativeElement;
    if (!root) return [];
    return Array.from(
      root.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'),
    );
  }

  private focusSelectedItem(): void {
    const items = this.menuItems();
    const current =
      items.find((b) => b.getAttribute('aria-checked') === 'true') ??
      items[0];
    current?.focus();
  }

  private closeMenuAndRefocus(): void {
    this.menuOpen.set(false);
    this.versionControl()
      ?.nativeElement.querySelector<HTMLButtonElement>(
        '[aria-haspopup="menu"]',
      )
      ?.focus();
  }

  protected close(): void {
    this.artifactState.closeArtifactPanel();
  }

  protected retry(): void {
    void this.load();
  }

  protected setView(next: 'preview' | 'code'): void {
    if (this.view() === next) return;
    this.view.set(next);
    if (next === 'code') void this.ensureSource();
  }

  protected retrySource(): void {
    this.loadedSourceKey = null;
    void this.ensureSource();
  }

  /** Fetch the raw source once per artifact version. No-op if it's
   *  already loaded for the current key or a fetch is in flight. */
  private async ensureSource(): Promise<void> {
    const ref = this.open();
    if (!ref) return;
    const key = this.currentKey();
    if (this.sourceLoading()) return;
    if (this.source() && this.loadedSourceKey === key) return;

    const seq = ++this.sourceSeq;
    this.sourceLoading.set(true);
    this.sourceError.set(null);
    this.source.set(null);
    try {
      const content = await this.artifactHttp.getArtifactContent(
        ref.artifactId,
        ref.version,
      );
      if (seq !== this.sourceSeq) return; // superseded — drop
      this.source.set(content);
      this.loadedSourceKey = key;
    } catch (err) {
      if (seq !== this.sourceSeq) return;
      this.sourceError.set(
        err instanceof HttpErrorResponse && err.status === 413
          ? 'This artifact is too large to preview here — download it instead.'
          : "This artifact's source couldn't be loaded. It may have expired or been removed.",
      );
    } finally {
      if (seq === this.sourceSeq) this.sourceLoading.set(false);
    }
  }

  protected async copy(): Promise<void> {
    const src = this.source();
    if (!src) return;
    try {
      await navigator.clipboard.writeText(src.content);
      this.copied.set(true);
      if (this.copiedTimer) clearTimeout(this.copiedTimer);
      this.copiedTimer = setTimeout(() => this.copied.set(false), 2000);
    } catch {
      /* clipboard blocked (permissions/insecure context) — no-op;
         the user can still select the visible source manually */
    }
  }

  private resetSource(): void {
    this.source.set(null);
    this.sourceError.set(null);
    this.sourceLoading.set(false);
    this.copied.set(false);
    this.loadedSourceKey = null;
  }

  protected async download(): Promise<void> {
    const ref = this.open();
    if (!ref || this.downloading()) return;
    this.downloading.set(true);
    try {
      await this.artifactDownload.download({
        artifactId: ref.artifactId,
        version: ref.version,
      });
    } finally {
      this.downloading.set(false);
    }
  }

  private reset(): void {
    this.safeUrl.set(null);
    this.error.set(null);
    this.loading.set(false);
    this.resetSource();
    this.view.set('preview');
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
