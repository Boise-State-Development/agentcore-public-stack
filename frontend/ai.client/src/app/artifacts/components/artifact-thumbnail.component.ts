import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroTableCells,
} from '@ng-icons/heroicons/outline';

import { ArtifactHttpService } from '../../session/services/artifacts/artifact-http.service';

/**
 * The virtual viewport an artifact is rendered at before being scaled
 * down to card size.
 *
 * Fixed rather than derived from the card, because the card is the wrong
 * size to render a document at: a 340px-wide iframe makes an HTML
 * artifact reflow into its mobile layout, so the thumbnail would show a
 * different design from the one the user gets when they open it. Render
 * at a desktop width and scale the *pixels* down, and the thumbnail is a
 * true miniature of the real thing.
 *
 * 16:10 because that is roughly what the artifact view page gives a
 * document, so the crop the card shows is the crop the viewer opens on.
 */
const VIRTUAL_WIDTH = 1024;
const VIRTUAL_HEIGHT = 640;

/**
 * Distance outside the viewport at which a card starts loading. One card
 * height of runway, so a preview is usually painted by the time it is
 * scrolled to, without loading the whole library up front.
 */
const PRELOAD_MARGIN_PX = 400;

/** Fallback glyph per content type, matching the library's type styling. */
const TYPE_ICONS: Record<string, string> = {
  'text/markdown': 'heroDocumentText',
  'text/x-markdown': 'heroDocumentText',
  'text/html': 'heroCodeBracket',
  'application/xhtml+xml': 'heroCodeBracket',
  'text/csv': 'heroTableCells',
  'image/svg+xml': 'heroPhoto',
};

/**
 * A live, scaled-down render of one artifact, for the library's grid
 * cards.
 *
 * ## Why a real iframe and not a screenshot
 *
 * The obvious way to build this is the way Claude does: rasterise each
 * artifact server-side and serve a PNG. That needs a headless-Chromium
 * Lambda, a bucket path, a backfill, and — the part that actually
 * decides it — executing user-authored HTML *server-side in our own
 * account*, which is a materially different threat model from the
 * client-side null-origin sandbox we already trust.
 *
 * Everything needed to render the artifact in the browser is already
 * built and deployed: an isolated artifact origin, a short-lived render
 * token, a strict CSP, and a `frame-ancestors` that already names the
 * SPA. So a thumbnail is the render path we have, scaled down — no new
 * AWS resources, no new IAM, no new way for artifact bytes to be
 * executed.
 *
 * ## Isolation
 *
 * Identical to `ArtifactViewerComponent`: `sandbox="allow-scripts"`
 * **without** `allow-same-origin`, so the framed document is a null
 * origin and cannot touch the artifact origin's storage or cookies. Do
 * not add `allow-same-origin` here either.
 *
 * The frame is additionally wrapped in an `inert` + `aria-hidden`
 * container. That is not decoration: a thumbnail is a picture, and
 * without it every card would inject a whole focusable document into the
 * page's tab order — a keyboard user would tab through the *contents* of
 * twelve artifacts to reach the next card. `inert` is what makes the
 * `aria-hidden` honest, since `aria-hidden` over focusable content is an
 * a11y defect on its own.
 *
 * ## Cost
 *
 * Every mounted preview is one mint (DynamoDB GetItem + an HS256 sign),
 * one render-Lambda invocation, and one S3 GetObject. The render path is
 * `CACHING_DISABLED` at CloudFront — it has to be, the token is in the
 * URL — so none of that is cached and a page view costs one round of it
 * per visible card.
 *
 * Two things keep that bounded. Previews mount lazily, on intersection,
 * so a library of any size costs only what is looked at. And a mounted
 * preview is never unmounted: scrolling back up must not re-mint and
 * re-invoke. The ceiling is therefore "artifacts the user actually
 * scrolled past", which is fine while `/artifacts/library` is
 * unpaginated (a user's whole library fits well inside one DynamoDB
 * page). If that endpoint ever gains paging, this needs LRU eviction of
 * off-screen frames to go with it.
 */
@Component({
  selector: 'app-artifact-thumbnail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  // The host *is* the frame: a block box with the aspect ratio the
  // virtual viewport is scaled into. `display: block` is load-bearing
  // rather than styling — an inline host measures 0 wide, which would
  // leave `scale()` pinned at 0 and every preview invisible.
  host: {
    class: 'relative block aspect-[16/10] overflow-hidden',
  },
  viewProviders: [
    provideIcons({
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroTableCells,
    }),
  ],
  template: `
    <div class="absolute inset-0 overflow-hidden bg-white dark:bg-gray-900">
      @if (safeUrl(); as url) {
        <!-- inert + aria-hidden: this is a picture of the artifact, not a
             copy of it. See the isolation note on the component. -->
        <div
          inert
          aria-hidden="true"
          class="absolute left-0 top-0 origin-top-left transition-opacity duration-300"
          [class.opacity-0]="!loaded()"
          [style.width.px]="virtualWidth"
          [style.height.px]="virtualHeight"
          [style.transform]="'scale(' + scale() + ')'"
        >
          <iframe
            [src]="url"
            title=""
            class="pointer-events-none h-full w-full border-0"
            sandbox="allow-scripts"
            referrerpolicy="no-referrer"
            loading="lazy"
            (load)="onLoad()"
          ></iframe>
        </div>
      }

      <!-- Placeholder underneath, revealed by the frame's opacity until it
           paints and left in place for good when there is nothing to show.
           A tinted glyph rather than a spinner: a preview that fails is not
           an error the user needs to act on, it is a card without a picture. -->
      @if (!loaded()) {
        <div
          class="absolute inset-0 grid place-items-center bg-gray-50 dark:bg-gray-900"
          aria-hidden="true"
        >
          <ng-icon [name]="fallbackIcon()" class="size-8 text-gray-300 dark:text-gray-600" />
        </div>
      }
    </div>
  `,
})
export class ArtifactThumbnailComponent implements AfterViewInit, OnDestroy {
  private readonly artifacts = inject(ArtifactHttpService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly artifactId = input.required<string>();
  readonly version = input.required<number>();
  readonly sessionId = input<string>('');
  readonly contentType = input<string>('');

  protected readonly virtualWidth = VIRTUAL_WIDTH;
  protected readonly virtualHeight = VIRTUAL_HEIGHT;

  protected readonly safeUrl = signal<SafeResourceUrl | null>(null);
  protected readonly loaded = signal(false);

  /** Card width, remeasured on resize. 0 until first measurement. */
  private readonly boxWidth = signal(0);

  /**
   * Scale that fits the virtual viewport to the card.
   *
   * Held at 0 before the first measurement so a freshly mounted frame is
   * not painted at full 1024px size for a frame or two — at three cards
   * per row that flash is the entire page jumping.
   */
  protected readonly scale = computed(() => this.boxWidth() / VIRTUAL_WIDTH);

  protected readonly fallbackIcon = computed(
    () => TYPE_ICONS[this.contentType().split(';')[0].trim().toLowerCase()] ?? 'heroDocument',
  );

  private intersectionObserver?: IntersectionObserver;
  private resizeObserver?: ResizeObserver;
  /** Guards the async mint against resolving into a torn-down view. */
  private destroyed = false;
  /**
   * Set the moment a mint is asked for, never cleared.
   *
   * A second mint would be a second render-Lambda invocation for a picture
   * already on screen, so "at most one per component" is enforced here
   * rather than left to the observer disconnecting — an observer can
   * deliver a batch, and the no-IntersectionObserver path has no observer
   * to rely on at all.
   */
  private requested = false;

  ngAfterViewInit(): void {
    const el = this.host.nativeElement;

    // Both observers are guarded on presence rather than assumed. They are
    // universally supported in browsers we target, but this component
    // renders inside every consumer's tests too, and a hard `new
    // ResizeObserver` in a lifecycle hook turns "your spec renders a card"
    // into "your spec throws". The degraded paths below are correct, not
    // just non-fatal.
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => this.boxWidth.set(el.clientWidth));
      this.resizeObserver.observe(el);
    }
    // Measure once regardless — without a ResizeObserver this is the only
    // measurement there will be, and with one it beats waiting a frame for
    // the observer's initial callback.
    this.boxWidth.set(el.clientWidth);

    // No IntersectionObserver: mint immediately rather than leaving a card
    // permanently blank. Costs more than lazy mounting; a preview that
    // never arrives is worse.
    if (typeof IntersectionObserver === 'undefined') {
      void this.mint();
      return;
    }

    this.intersectionObserver = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) {
          return;
        }
        // One-shot: a mounted preview is never torn down, so there is
        // nothing left to observe once it has been asked for.
        this.intersectionObserver?.disconnect();
        this.intersectionObserver = undefined;
        void this.mint();
      },
      { rootMargin: `${PRELOAD_MARGIN_PX}px` },
    );
    this.intersectionObserver.observe(el);
  }

  ngOnDestroy(): void {
    this.intersectionObserver?.disconnect();
    this.resizeObserver?.disconnect();
    this.destroyed = true;
  }

  protected onLoad(): void {
    this.loaded.set(true);
  }

  /**
   * Mint a render URL for this artifact's HEAD.
   *
   * A failure is deliberately silent: the placeholder is already on
   * screen and stays there, so a card whose preview could not be minted
   * degrades to the card this page shipped with rather than growing an
   * error the user cannot act on. The real errors — a deleted or
   * unreadable artifact — surface when they open it.
   */
  private async mint(): Promise<void> {
    if (this.requested) {
      return;
    }
    this.requested = true;
    try {
      const token = await this.artifacts.mintRenderToken(
        this.artifactId(),
        this.version(),
        this.sessionId(),
      );
      if (this.destroyed) {
        return;
      }
      this.safeUrl.set(this.sanitizer.bypassSecurityTrustResourceUrl(token.url));
    } catch {
      /* no preview for this card — the placeholder stands */
    }
  }
}
