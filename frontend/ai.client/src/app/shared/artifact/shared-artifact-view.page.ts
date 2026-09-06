import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroLockClosed,
  heroExclamationTriangle,
  heroArrowDownTray,
  heroArrowPath,
  heroEye,
  heroCodeBracket,
  heroClipboard,
  heroCheck,
} from '@ng-icons/heroicons/outline';
import {
  ArtifactShareService,
  type SharedArtifact,
} from '../../session/services/artifacts/artifact-share.service';
import { ArtifactDownloadService } from '../../session/services/artifacts/artifact-download.service';
import { ArtifactViewerComponent } from '../../session/components/message-list/components/artifact/artifact-viewer.component';
import type { ArtifactContent } from '../../session/services/artifacts/artifact-http.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * Recipient view for a shared artifact, at `/shared-artifact/:shareId`.
 *
 * Modelled on `shared-view.page.ts` (the conversation equivalent): the
 * same sticky read-only banner and the same 403 / 404 / other error
 * branches, so a recipient sees one consistent story whichever kind of
 * link they were sent.
 *
 * It renders through the same `ArtifactViewerComponent` the owner's
 * docked panel uses — same sandboxed iframe, same code view — but every
 * call goes to the access-checked `/shared-artifacts/…` endpoints. It
 * never learns the artifact id: the share id is the only handle it has,
 * and the backend resolves the owner behind the ACL.
 *
 * Read-only by construction. There is no share button, no version menu
 * (a share pins one immutable version), and no way to edit.
 */
@Component({
  selector: 'app-shared-artifact-view',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    DatePipe,
    RouterLink,
    ArtifactViewerComponent,
    TooltipDirective,
  ],
  providers: [
    provideIcons({
      heroLockClosed,
      heroExclamationTriangle,
      heroArrowDownTray,
      heroArrowPath,
      heroEye,
      heroCodeBracket,
      heroClipboard,
      heroCheck,
    }),
  ],
  template: `
    <div class="flex h-full min-h-0 flex-col bg-white dark:bg-gray-900">
      <!-- Read-only banner: the same promise the shared-conversation
           page makes, so the two recipient surfaces read alike. -->
      <div
        class="border-b border-gray-200 bg-white/95 backdrop-blur dark:border-gray-700 dark:bg-gray-900/95"
      >
        <div class="flex items-center gap-3 px-4 py-3">
          <!-- With the shell chrome stripped there is no sidenav to
               navigate from, so this is the recipient's only way into
               the app. Deliberately understated — one link, not a nav. -->
          <a
            routerLink="/"
            class="shrink-0 rounded-md px-2 py-1 text-xs font-semibold text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
          >
            boisestate.ai
          </a>
          <div class="flex flex-1 items-center justify-center gap-2">
            <ng-icon
              name="heroLockClosed"
              class="size-4 text-gray-400"
              aria-hidden="true"
            />
            <span
              class="text-xs font-medium text-gray-500 dark:text-gray-400"
              >Shared read-only snapshot</span
            >
            @if (artifact(); as a) {
              <span class="text-xs text-gray-400 dark:text-gray-500">
                · {{ a.createdAt | date: 'medium' }}
              </span>
            }
          </div>
          <!-- Balances the link so the banner text stays optically
               centred; width tracks the link via the same font metrics. -->
          <span class="w-[6.5rem] shrink-0" aria-hidden="true"></span>
        </div>
      </div>

      @if (isLoading()) {
        <div class="flex flex-1 items-center justify-center py-20">
          <div class="text-center">
            <div
              class="mx-auto size-8 animate-spin rounded-full border-2 border-gray-300 border-t-primary-600"
            ></div>
            <p class="mt-3 text-sm text-gray-500 dark:text-gray-400">
              Loading shared artifact…
            </p>
          </div>
        </div>
      } @else if (errorStatus()) {
        <div class="flex flex-1 items-center justify-center py-20">
          <div class="text-center">
            <ng-icon
              name="heroExclamationTriangle"
              class="mx-auto size-12 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            @if (errorStatus() === 403) {
              <h1 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">
                Access denied
              </h1>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                You don't have permission to view this artifact.
              </p>
            } @else if (errorStatus() === 404) {
              <h1 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">
                Artifact not found
              </h1>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                This share link may have been revoked.
              </p>
            } @else {
              <h1 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">
                Something went wrong
              </h1>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                Failed to load the shared artifact.
              </p>
            }
          </div>
        </div>
      } @else if (artifact(); as a) {
        <header
          class="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700"
        >
          <div class="min-w-0 flex-1">
            <h1
              class="truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            >
              {{ a.title || 'Untitled artifact' }}
            </h1>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              Version {{ a.version }} · shared by {{ a.ownerEmail }}
            </p>
          </div>

          <div
            role="group"
            aria-label="Artifact view mode"
            class="flex items-center gap-0.5 rounded-md border border-gray-200 p-0.5 dark:border-gray-700"
          >
            <button
              type="button"
              class="flex size-7 items-center justify-center rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible"
              [class]="
                view() === 'preview'
                  ? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100'
                  : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              "
              [attr.aria-pressed]="view() === 'preview'"
              aria-label="Preview"
              [appTooltip]="'Preview'"
              appTooltipPosition="bottom"
              (click)="setView('preview')"
            >
              <ng-icon name="heroEye" class="text-base" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="flex size-7 items-center justify-center rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible"
              [class]="
                view() === 'code'
                  ? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100'
                  : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              "
              [attr.aria-pressed]="view() === 'code'"
              aria-label="View code"
              [appTooltip]="'View code'"
              appTooltipPosition="bottom"
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
              class="flex size-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
              [attr.aria-label]="copied() ? 'Copied' : 'Copy code'"
              [appTooltip]="copied() ? 'Copied' : 'Copy code'"
              appTooltipPosition="bottom"
              [disabled]="!source()"
              (click)="copy()"
            >
              <ng-icon
                [name]="copied() ? 'heroCheck' : 'heroClipboard'"
                class="text-lg"
                [class.text-state-success-600]="copied()"
                aria-hidden="true"
              />
            </button>
          }

          @if (a.canDownload) {
            <button
              type="button"
              class="flex size-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
              [attr.aria-label]="
                downloading() ? 'Downloading artifact…' : 'Download artifact'
              "
              [appTooltip]="'Download'"
              appTooltipPosition="bottom"
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
          }
        </header>

        <div class="relative flex min-h-0 flex-1 flex-col">
          <app-artifact-viewer
            [safeUrl]="safeUrl()"
            [title]="a.title"
            [view]="view()"
            [source]="source()"
            [error]="renderError()"
            [sourceError]="sourceError()"
            [previewReady]="previewReady()"
            (retry)="retry()"
            (retrySource)="retrySource()"
            (iframeLoad)="onIframeLoad()"
          />
        </div>
      }
    </div>
  `,
})
export class SharedArtifactViewPage implements OnInit {
  private route = inject(ActivatedRoute);
  private shareService = inject(ArtifactShareService);
  private downloadService = inject(ArtifactDownloadService);
  private sanitizer = inject(DomSanitizer);

  private shareId = '';

  protected readonly artifact = signal<SharedArtifact | null>(null);
  protected readonly isLoading = signal(true);
  protected readonly errorStatus = signal<number | null>(null);

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly renderError = signal<string | null>(null);
  protected readonly iframeLoaded = signal(false);
  protected readonly previewReady = computed(
    () => !!this.safeUrl() && this.iframeLoaded(),
  );

  protected readonly view = signal<'preview' | 'code'>('preview');
  protected readonly source = signal<ArtifactContent | null>(null);
  protected readonly sourceError = signal<string | null>(null);
  private sourceLoading = false;

  protected readonly copied = signal(false);
  protected readonly downloading = signal(false);
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

  /** Bumped per mint so a slow response that resolves after a retry is
   *  discarded rather than overwriting the newer one. */
  private requestSeq = 0;

  async ngOnInit(): Promise<void> {
    const shareId = this.route.snapshot.paramMap.get('shareId');
    if (!shareId) {
      this.errorStatus.set(404);
      this.isLoading.set(false);
      return;
    }
    this.shareId = shareId;

    try {
      // Metadata first: it is the access check. A 403 or 404 here means
      // no token is ever minted.
      this.artifact.set(await this.shareService.getSharedArtifact(shareId));
    } catch (err: unknown) {
      this.errorStatus.set(statusOf(err));
      this.isLoading.set(false);
      return;
    }
    this.isLoading.set(false);
    await this.mint();
  }

  protected retry(): void {
    void this.mint();
  }

  protected onIframeLoad(): void {
    this.iframeLoaded.set(true);
  }

  protected setView(next: 'preview' | 'code'): void {
    if (this.view() === next) return;
    this.view.set(next);
    if (next === 'code') void this.ensureSource();
  }

  protected retrySource(): void {
    this.source.set(null);
    this.sourceError.set(null);
    void this.ensureSource();
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
      /* clipboard blocked (permissions/insecure context) — the user can
         still select the visible source manually */
    }
  }

  protected async download(): Promise<void> {
    if (this.downloading()) return;
    this.downloading.set(true);
    try {
      // Recipients have no artifact id to mint against; the share id is
      // the authority, and the download service routes it through the
      // access-checked endpoint.
      await this.downloadService.download({ shareId: this.shareId });
    } finally {
      this.downloading.set(false);
    }
  }

  /** Mint a fresh render URL. The token is a ~120s bearer credential —
   *  never cached, re-minted on retry. */
  private async mint(): Promise<void> {
    const seq = ++this.requestSeq;
    this.renderError.set(null);
    this.safeUrl.set(null);
    this.iframeLoaded.set(false);
    try {
      const token = await this.shareService.mintSharedRenderToken(
        this.shareId,
      );
      if (seq !== this.requestSeq) return; // superseded — drop
      this.safeUrl.set(
        this.sanitizer.bypassSecurityTrustResourceUrl(token.url),
      );
    } catch (err: unknown) {
      if (seq !== this.requestSeq) return;
      const status = statusOf(err);
      // A share revoked while the page was open stops being a render
      // problem and becomes a dead link — say so on the page, not in a
      // retryable error box inside a viewer that will never load.
      if (status === 403 || status === 404) {
        this.errorStatus.set(status);
        this.artifact.set(null);
        return;
      }
      this.renderError.set(
        "This artifact couldn't be loaded. The link may have expired or been revoked.",
      );
    }
  }

  /** Fetch the raw source once. No-op while a fetch is in flight or the
   *  source is already loaded — the version is pinned, so it can't go
   *  stale under us the way the owner panel's can. */
  private async ensureSource(): Promise<void> {
    if (this.sourceLoading || this.source()) return;
    this.sourceLoading = true;
    this.sourceError.set(null);
    try {
      this.source.set(
        await this.shareService.getSharedArtifactContent(this.shareId),
      );
    } catch (err: unknown) {
      this.sourceError.set(
        err instanceof HttpErrorResponse && err.status === 413
          ? 'This artifact is too large to preview here — download it instead.'
          : "This artifact's source couldn't be loaded. It may have expired or been removed.",
      );
    } finally {
      this.sourceLoading = false;
    }
  }
}

/** HTTP status of a failed call, defaulting to 500 for anything that
 *  didn't reach the server (offline, DNS, aborted). */
function statusOf(err: unknown): number {
  return (err as { status?: number } | null)?.status ?? 500;
}
