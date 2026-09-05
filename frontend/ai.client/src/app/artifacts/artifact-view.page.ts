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
  heroArrowDownTray,
  heroArrowLeft,
  heroArrowPath,
  heroArrowTopRightOnSquare,
  heroChatBubbleLeftRight,
  heroCheck,
  heroClipboard,
  heroCodeBracket,
  heroExclamationTriangle,
  heroEye,
} from '@ng-icons/heroicons/outline';

import {
  ArtifactHttpService,
  type ArtifactContent,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../session/services/artifacts/artifact-download.service';
import { ArtifactViewerComponent } from '../session/components/message-list/components/artifact/artifact-viewer.component';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';

/**
 * Owner-facing artifact viewer at `/artifacts/:artifactId`.
 *
 * This exists because the library's original open path did not survive
 * contact with reality. It minted a render token and handed it to
 * `window.open`, which made viewing your own artifact contingent on a
 * pop-up — and a pop-up is the one thing a browser is entitled to refuse.
 * Anyone with a blocker, and every embedded webview (the Claude Code
 * browser pane blocks `window.open` unconditionally, feature string or
 * not), got a toast instead of their document. A viewer that a browser
 * setting can switch off is not a viewer.
 *
 * So opening is now in-app navigation, which nothing can block. "Open in
 * a new tab" survives as a secondary action *inside* this page, where a
 * blocked pop-up costs the user nothing: the artifact is already on
 * screen next to it.
 *
 * Modelled on `shared-artifact-view.page.ts` and rendering through the
 * same `ArtifactViewerComponent` as the docked panel, so the owner and
 * recipient surfaces stay recognisably the same thing. The differences
 * are ownership-shaped: this one mints against the artifact id rather
 * than a share id, offers a route back to the library, and links to the
 * conversation that produced it.
 */
@Component({
  selector: 'app-artifact-view',
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
      heroArrowDownTray,
      heroArrowLeft,
      heroArrowPath,
      heroArrowTopRightOnSquare,
      heroChatBubbleLeftRight,
      heroCheck,
      heroClipboard,
      heroCodeBracket,
      heroExclamationTriangle,
      heroEye,
    }),
  ],
  templateUrl: './artifact-view.page.html',
})
export class ArtifactViewPage implements OnInit {
  private route = inject(ActivatedRoute);
  private artifacts = inject(ArtifactHttpService);
  private downloadService = inject(ArtifactDownloadService);
  private sanitizer = inject(DomSanitizer);

  protected readonly artifact = signal<LibraryArtifact | null>(null);
  protected readonly isLoading = signal(true);
  protected readonly notFound = signal(false);
  protected readonly loadFailed = signal(false);

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly renderError = signal<string | null>(null);
  private readonly iframeLoaded = signal(false);
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
    const artifactId = this.route.snapshot.paramMap.get('artifactId');
    if (!artifactId) {
      this.notFound.set(true);
      this.isLoading.set(false);
      return;
    }

    // Read the record out of the library listing rather than adding a
    // get-one endpoint. The listing is deliberately unpaginated because a
    // single user's artifacts sit far under one DynamoDB page, so this is
    // one small call — and it keeps the viewer shipping without a backend
    // deploy to sequence against. If that endpoint ever gains paging,
    // this needs a real `GET /artifacts/{id}` behind it.
    try {
      const found = (await this.artifacts.listLibrary()).find(
        (a) => a.artifactId === artifactId,
      );
      if (!found) {
        this.notFound.set(true);
        this.isLoading.set(false);
        return;
      }
      this.artifact.set(found);
    } catch {
      this.loadFailed.set(true);
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
    const a = this.artifact();
    if (!a || this.downloading()) return;
    this.downloading.set(true);
    try {
      await this.downloadService.download({
        artifactId: a.artifactId,
        version: a.version,
      });
    } finally {
      this.downloading.set(false);
    }
  }

  /**
   * Secondary affordance only. Unlike the library's old primary action,
   * a refusal here is harmless — the artifact is already rendered on this
   * page — so a blocked pop-up needs no error, and gets none.
   */
  protected openInNewTab(): void {
    const url = this.rawUrl;
    if (!url) return;
    const tab = window.open('', '_blank');
    if (!tab) return;
    try {
      tab.opener = null;
    } catch {
      /* read-only in some embedded webviews; destination is our origin */
    }
    tab.location.href = url;
  }

  /** The unsanitized render URL, kept for the new-tab affordance —
   *  `SafeResourceUrl` is opaque and cannot be read back out. */
  private rawUrl: string | null = null;

  /** Mint a fresh render URL. The token is a ~120s bearer credential —
   *  never cached, re-minted on retry. */
  private async mint(): Promise<void> {
    const a = this.artifact();
    if (!a) return;
    const seq = ++this.requestSeq;
    this.renderError.set(null);
    this.safeUrl.set(null);
    this.rawUrl = null;
    this.iframeLoaded.set(false);
    try {
      const token = await this.artifacts.mintRenderToken(
        a.artifactId,
        a.version,
        a.sessionId,
      );
      if (seq !== this.requestSeq) return; // superseded — drop
      this.rawUrl = token.url;
      this.safeUrl.set(
        this.sanitizer.bypassSecurityTrustResourceUrl(token.url),
      );
    } catch {
      if (seq !== this.requestSeq) return;
      this.renderError.set(
        "This artifact couldn't be loaded. Try again in a moment.",
      );
    }
  }

  /** Fetch the raw source once. No-op while a fetch is in flight or the
   *  source is already loaded. */
  private async ensureSource(): Promise<void> {
    const a = this.artifact();
    if (!a || this.sourceLoading || this.source()) return;
    this.sourceLoading = true;
    this.sourceError.set(null);
    try {
      this.source.set(
        await this.artifacts.getArtifactContent(a.artifactId, a.version),
      );
    } catch (err: unknown) {
      this.sourceError.set(
        err instanceof HttpErrorResponse && err.status === 413
          ? 'This artifact is too large to preview here — download it instead.'
          : "This artifact's source couldn't be loaded.",
      );
    } finally {
      this.sourceLoading = false;
    }
  }
}
