import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

/**
 * Width, in CSS px, the deck is rendered at before it is scaled to fit.
 *
 * `pptx-preview` takes a fixed pixel size at `init()` and lays the deck
 * out against it — it has no responsive mode. Re-initialising on every
 * frame of a rail drag would re-parse the whole file, so instead the
 * deck is rendered once at this width and CSS-zoomed to fit, exactly as
 * the docx viewer scales its pages.
 *
 * 960 is a deliberate over-render: comfortably wider than the rail gets,
 * so fitting always scales *down* and text stays sharp rather than being
 * enlarged from an undersized layout.
 */
const RENDER_WIDTH_PX = 960;

/** 16:9, the aspect ratio `create_powerpoint_presentation` always emits. */
const RENDER_HEIGHT_PX = Math.round((RENDER_WIDTH_PX * 9) / 16);

/**
 * Renders a `.pptx` in the browser, from its raw bytes.
 *
 * Uses `pptx-preview`, which reads the OOXML and reproduces each slide's
 * own geometry — shape positions, fills, text frames, tables, images and
 * the theme inherited from slide masters and layouts — as absolutely
 * positioned DOM. That last part is what makes it worth a dependency:
 * decks built on an uploaded corporate template get their branding from
 * the master, and a renderer that only walked the slides would drop it.
 *
 * Nothing leaves the browser, for the same reason as the docx viewer —
 * the alternative is a server-side render at Microsoft, which needs the
 * file reachable by an unauthenticated URL.
 *
 * The library is loaded with a dynamic `import()`: it is dead weight in
 * the initial bundle for the large majority of sessions that never open
 * a deck, and it touches `document` at module scope, so a static import
 * would run during SSR.
 *
 * Native OOXML charts are deliberately not supported — see
 * `shims/echarts-stub/README.md` for why, and what a deck containing one
 * does.
 */
@Component({
  selector: 'app-pptx-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[style.--pptx-zoom]': 'zoom()' },
  template: `
    <div
      #scroller
      class="h-full overflow-auto bg-gray-100 px-4 py-6 dark:bg-gray-950"
    >
      <!-- pptx-preview builds this subtree imperatively, so it must be a
           plain element Angular does not also manage. -->
      <div #deckHost class="pptx-host"></div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }

    /* The library builds its subtree imperatively, so it never receives
       Angular's emulated-encapsulation attribute and cannot be reached
       by an ordinary rule here. ::ng-deep under :host is the supported
       escape hatch and keeps the selector anchored to this component.

       The !important declarations below are load-bearing, not
       defensive: the library writes those three properties as *inline*
       styles on the wrapper — a black backdrop, a fixed pixel height
       sized for its own internal scroller, and an overflow that drives
       it — and an inline style beats any selector we can write here.
       Without them the deck sits in a black letterbox inside a second,
       nested scrollbar. */
    :host ::ng-deep .pptx-preview-wrapper {
      /* Transparent so the scroller's themed gutter shows through,
         rather than the library's black. */
      background: transparent !important;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.5rem;
      /* CSS zoom rather than transform: scale() — zoom participates in
         layout, so the flow collapses to the scaled height on its own. A
         transform would paint smaller while still reserving the
         unscaled height, leaving dead space under every slide. Same
         reasoning as the docx viewer. */
      zoom: var(--pptx-zoom, 1);
      /* The pane is the scroller, so let the content decide the height
         and scroll the pane rather than a box inside it. */
      height: auto !important;
      overflow: visible !important;
    }

    /* Each slide. Kept on a white card in both themes: this previews
       something the user will present or export, and recolouring a slide
       would misrepresent the deck. The gutter carries the theme. */
    :host ::ng-deep .pptx-preview-slide-wrapper {
      background: var(--color-white);
      box-shadow:
        0 1px 3px rgb(0 0 0 / 0.12),
        0 1px 2px rgb(0 0 0 / 0.08);
      border-radius: 0.125rem;
      flex: none;
      max-width: 100%;
    }
  `,
})
export class PptxViewerComponent {
  /** Raw `.pptx` bytes. Null clears the view (pane closed / still loading). */
  readonly bytes = input<ArrayBuffer | null>(null);

  /** The deck painted. */
  readonly rendered = output<void>();

  /** Rendering failed — the bytes were not a readable `.pptx`. */
  readonly renderFailed = output<string>();

  private readonly deckHost =
    viewChild.required<ElementRef<HTMLElement>>('deckHost');
  private readonly scroller =
    viewChild.required<ElementRef<HTMLElement>>('scroller');

  /** Scale applied to the deck so a full slide fits the pane width.
   *  Capped at 1 — see `applyFit`. */
  protected readonly zoom = signal(1);

  /** Bumped per render so a slow parse that resolves after the input
   *  changed (or the pane closed) cannot paint over the current deck. */
  private renderSeq = 0;

  /** Cached module so reopening the pane doesn't re-request the chunk. */
  private static libraryPromise: Promise<
    typeof import('pptx-preview')
  > | null = null;

  constructor() {
    // Re-fit when the rail is resized. The deck's own layout is fixed at
    // render time, so this only adjusts the zoom — no re-parse. Guarded
    // for SSR, where there is no ResizeObserver and no layout to fit to.
    effect((onCleanup) => {
      if (typeof ResizeObserver === 'undefined') return;
      const ro = new ResizeObserver(() => this.applyFit());
      ro.observe(this.scroller().nativeElement);
      onCleanup(() => ro.disconnect());
    });

    effect(() => {
      const data = this.bytes();
      const host = this.deckHost().nativeElement;

      const seq = ++this.renderSeq;

      if (!data) {
        host.replaceChildren();
        this.zoom.set(1);
        return;
      }

      void this.render(data, host, seq);
    });
  }

  private async render(
    data: ArrayBuffer,
    host: HTMLElement,
    seq: number,
  ): Promise<void> {
    // Render into a detached container and swap it in only on success,
    // so two overlapping renders cannot interleave their output into the
    // live host — the sequence guard below cannot undo DOM the library
    // wrote on its own.
    const target = document.createElement('div');

    try {
      PptxViewerComponent.libraryPromise ??= import('pptx-preview');
      const { init } = await PptxViewerComponent.libraryPromise;
      if (seq !== this.renderSeq) return;

      const previewer = init(target, {
        width: RENDER_WIDTH_PX,
        height: RENDER_HEIGHT_PX,
        // Every slide stacked in one scrollable column, matching how the
        // docx viewer presents pages. 'slide' would paginate with the
        // library's own prev/next chrome, which duplicates the pane's
        // scrollbar and reads as a second, competing set of controls.
        mode: 'list',
      });
      await previewer.preview(data);

      if (seq !== this.renderSeq) return;

      // A deck that parsed but produced no slides is a failure, not an
      // empty document. `pptx-preview` resolves successfully when it
      // cannot make sense of a presentation's theme or layout parts —
      // observed on a deck with a stripped-down theme, which returned
      // zero slides without throwing. Left unchecked that paints an
      // empty pane with no error and no retry, which reads as the app
      // being broken rather than the file being unreadable.
      if (previewer.slideCount === 0) {
        throw new Error('pptx-preview produced no slides');
      }

      host.replaceChildren(target);
      this.applyFit();
      this.rendered.emit();
    } catch {
      if (seq !== this.renderSeq) return;
      host.replaceChildren();
      this.renderFailed.emit(
        "This file couldn't be read as a PowerPoint presentation. It may be corrupted or saved in an older format.",
      );
    }
  }

  /**
   * Scale the deck so one full slide fits the pane's content box.
   *
   * Never scales above 1: the deck is laid out at `RENDER_WIDTH_PX`,
   * wider than the rail goes, so enlarging would only magnify a layout
   * that is already the reference size.
   */
  private applyFit(): void {
    const el = this.scroller().nativeElement;
    const styles = getComputedStyle(el);
    const available =
      el.clientWidth -
      parseFloat(styles.paddingLeft || '0') -
      parseFloat(styles.paddingRight || '0');

    if (available <= 0) {
      this.zoom.set(1);
      return;
    }
    this.zoom.set(Math.min(1, available / RENDER_WIDTH_PX));
  }
}
