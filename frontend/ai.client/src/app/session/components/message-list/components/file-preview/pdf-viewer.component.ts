import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  RenderTask,
  TextLayer,
} from 'pdfjs-dist';
import { PdfJsLoader, type PdfJs } from './pdfjs-loader';

/**
 * Highest device pixel ratio a page canvas is drawn at. A canvas costs
 * width × height × 4 bytes, so a full-width page at 3× on a phone is
 * several times the memory of 2× for no difference anyone can see in a
 * side pane.
 */
const MAX_PIXEL_RATIO = 2;

/**
 * How far outside the visible part of the pane a page is drawn ahead of
 * the scroll, and past which a drawn page gives its canvas back. One pane
 * height either side keeps scrolling smooth without holding every page of
 * a long document in memory.
 */
const RENDER_MARGIN = '100% 0px';

/** Resize events are coalesced for this long before pages are redrawn. */
const RESIZE_DEBOUNCE_MS = 150;

/** One page's slot in the scroll column. */
interface PageSlot {
  /** 1-based, as pdf.js numbers them. */
  readonly pageNumber: number;
  readonly el: HTMLDivElement;
  /** Fit width (CSS px) the slot was last drawn at, or null if not drawn. */
  drawnWidth: number | null;
  /** Fit width of a draw in flight, so a second request does not repeat it. */
  pendingWidth: number | null;
  task: RenderTask | null;
  textLayer: TextLayer | null;
  visible: boolean;
}

/**
 * Renders a PDF in the browser, from its raw bytes, with pdf.js.
 *
 * Pages stack in one scrolling column fitted to the pane's width, like
 * the docx and pptx viewers, so the pane's own scrollbar is the only
 * navigation. Each page is a canvas with pdf.js's text layer over it:
 * transparent, positioned text that makes the page selectable and
 * copyable, and readable by a screen reader, which a canvas alone is not.
 *
 * Pages are drawn only near the visible part of the pane and give their
 * canvas back once scrolled well away, so a 400-page report costs about
 * what a short one does. Until a page is drawn its slot takes the first
 * page's proportions, which is right for nearly every document; a page
 * of a different size corrects its slot when it is drawn.
 *
 * pdf.js rather than the browser's built-in viewer: the built-in one
 * shows only the first page of a framed PDF on iOS Safari, and it needs
 * a `blob:` source in the app's `frame-src`. Nothing leaves the browser.
 * The bytes are parsed in pdf.js's worker, and no script in the PDF ever
 * runs: scripting needs pdf.js's separate sandbox bundle, which is not
 * loaded.
 *
 * The library is loaded with a dynamic `import()` (see `PdfJsLoader`):
 * it is dead weight in the initial bundle for sessions that never open a
 * PDF, and it touches `document` at module scope.
 */
@Component({
  selector: 'app-pdf-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      #scroller
      class="h-full overflow-auto bg-gray-100 px-4 py-6 dark:bg-gray-950"
    >
      <!-- Pages are built imperatively (each is a canvas pdf.js draws
           into), so this must be a plain element Angular does not also
           manage. -->
      <div
        #pagesHost
        class="pdf-pages"
        role="document"
        [attr.aria-label]="pageCount() ? pageCountLabel() : null"
      ></div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }

    .pdf-pages {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.5rem;
    }

    /* The page elements are created imperatively, so they never receive
       Angular's emulated-encapsulation attribute. ::ng-deep under :host
       is the supported way to reach them and keeps the rules anchored to
       this component. */

    /* Each page. Kept on a white card in both themes, like the pptx
       viewer's slides: recolouring a page would misrepresent the
       document. The gutter carries the theme. */
    :host ::ng-deep .pdf-page {
      position: relative;
      width: 100%;
      background: var(--color-white);
      box-shadow:
        0 1px 3px rgb(0 0 0 / 0.12),
        0 1px 2px rgb(0 0 0 / 0.08);
      border-radius: 0.125rem;
      flex: none;
      overflow: hidden;
      --scale-round-x: 1px;
      --scale-round-y: 1px;
    }

    :host ::ng-deep .pdf-page canvas {
      display: block;
      width: 100%;
      height: 100%;
    }

    /* pdf.js's text layer, from its own pdf_viewer.css (6.5.438), cut to
       the rules a read-only page uses: no find highlighting, no editing.
       The spans are transparent and sized by --total-scale-factor, which
       each page sets to the scale it was drawn at. */
    :host ::ng-deep .textLayer {
      color-scheme: only light;
      position: absolute;
      text-align: initial;
      inset: 0;
      overflow: clip;
      opacity: 1;
      line-height: 1;
      letter-spacing: normal;
      word-spacing: normal;
      text-size-adjust: none;
      forced-color-adjust: none;
      transform-origin: 0 0;
      caret-color: CanvasText;
      z-index: 0;
      --min-font-size: 1;
      --text-scale-factor: calc(var(--total-scale-factor) * var(--min-font-size));
      --min-font-size-inv: calc(1 / var(--min-font-size));
    }

    :host ::ng-deep .textLayer :is(span, br) {
      color: transparent;
      position: absolute;
      white-space: pre;
      cursor: text;
      transform-origin: 0% 0%;
      user-select: text;
    }

    :host ::ng-deep .textLayer > :not(.markedContent),
    :host ::ng-deep .textLayer .markedContent span:not(.markedContent) {
      z-index: 1;
      --font-height: 0;
      font-size: calc(var(--text-scale-factor) * var(--font-height));
      --scale-x: 1;
      --rotate: 0deg;
      transform: rotate(var(--rotate)) scaleX(var(--scale-x))
        scale(var(--min-font-size-inv));
    }

    :host ::ng-deep .textLayer .markedContent {
      display: contents;
    }

    :host ::ng-deep .textLayer span[role='img'] {
      user-select: none;
      cursor: default;
    }

    :host ::ng-deep .textLayer ::selection {
      background: rgb(0 0 255 / 0.25);
      background: color-mix(in srgb, AccentColor, transparent 50%);
      color: transparent;
    }

    :host ::ng-deep .textLayer br::selection {
      background: transparent;
    }

    :host ::ng-deep .textLayer .endOfContent {
      display: block;
      position: absolute;
      inset: 100% 0 0;
      z-index: 0;
      cursor: default;
      user-select: none;
    }

    :host ::ng-deep .textLayer.selecting .endOfContent {
      top: 0;
    }
  `,
})
export class PdfViewerComponent {
  /** Raw PDF bytes. Null clears the view (pane closed / still loading). */
  readonly bytes = input<ArrayBuffer | null>(null);

  /** The first page painted. */
  readonly rendered = output<void>();

  /** The bytes could not be opened as a PDF. */
  readonly renderFailed = output<string>();

  private readonly loader = inject(PdfJsLoader);

  private readonly pagesHost =
    viewChild.required<ElementRef<HTMLElement>>('pagesHost');
  private readonly scroller =
    viewChild.required<ElementRef<HTMLElement>>('scroller');

  protected readonly pageCount = signal(0);
  protected pageCountLabel(): string {
    const n = this.pageCount();
    return n === 1 ? 'PDF, 1 page' : `PDF, ${n} pages`;
  }

  /** Bumped per document so a slow open that resolves after the input
   *  changed (or the pane closed) cannot paint over the current one. */
  private renderSeq = 0;

  private pdfjs: PdfJs | null = null;
  private loadingTask: PDFDocumentLoadingTask | null = null;
  private doc: PDFDocumentProxy | null = null;
  private slots: PageSlot[] = [];
  private visibility: IntersectionObserver | null = null;
  private resizeTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFitWidth = 0;

  constructor() {
    // Redraw at the new width when the rail is resized. Slots follow the
    // width on their own (CSS), so between the resize and the redraw a
    // page is only briefly soft, never misplaced.
    effect((onCleanup) => {
      if (typeof ResizeObserver === 'undefined') return;
      const ro = new ResizeObserver(() => this.scheduleResize());
      ro.observe(this.scroller().nativeElement);
      onCleanup(() => ro.disconnect());
    });

    effect(() => {
      const data = this.bytes();
      const host = this.pagesHost().nativeElement;
      const seq = ++this.renderSeq;

      this.teardown(host);
      if (!data) return;
      void this.open(data, host, seq);
    });

    inject(DestroyRef).onDestroy(() => {
      this.renderSeq++;
      if (this.resizeTimer) clearTimeout(this.resizeTimer);
      this.teardown(null);
    });
  }

  private async open(data: ArrayBuffer, host: HTMLElement, seq: number): Promise<void> {
    try {
      const pdfjs = await this.loader.load();
      if (seq !== this.renderSeq) return;
      this.pdfjs = pdfjs;

      // pdf.js transfers a typed array to its worker, which detaches the
      // buffer it views. The panel still holds `data` (a retry hands the
      // same bytes back), so the worker gets a copy.
      const task = pdfjs.getDocument({
        data: new Uint8Array(data.slice(0)),
        cMapUrl: this.loader.assetUrl('cmaps'),
        standardFontDataUrl: this.loader.assetUrl('standard_fonts'),
        wasmUrl: this.loader.assetUrl('wasm'),
        iccUrl: this.loader.assetUrl('iccs'),
        // Forms are drawn as they appear; XFA (a separate, dynamic form
        // format) stays off, as it is by default.
        enableXfa: false,
      });
      this.loadingTask = task;
      const doc = await task.promise;
      // A newer document's teardown has already destroyed this task.
      if (seq !== this.renderSeq) return;
      this.doc = doc;

      if (doc.numPages === 0) throw new Error('PDF has no pages');

      // Size every slot from the first page, then draw it straight away:
      // the panel's skeleton stays up until it is on screen.
      const first = await doc.getPage(1);
      if (seq !== this.renderSeq) return;
      const base = first.getViewport({ scale: 1 });
      this.buildSlots(host, doc.numPages, base.width, base.height);
      this.pageCount.set(doc.numPages);

      this.lastFitWidth = this.fitWidth();
      await this.drawPage(this.slots[0], seq);
      if (seq !== this.renderSeq) return;

      this.observeVisibility(seq);
      this.rendered.emit();
    } catch (error) {
      if (seq !== this.renderSeq) return;
      this.teardown(host);
      this.renderFailed.emit(failureMessage(error));
    }
  }

  private buildSlots(
    host: HTMLElement,
    count: number,
    width: number,
    height: number,
  ): void {
    const fragment = document.createDocumentFragment();
    this.slots = [];
    for (let i = 1; i <= count; i++) {
      const el = document.createElement('div');
      el.className = 'pdf-page';
      el.setAttribute('role', 'group');
      el.setAttribute('aria-label', `Page ${i} of ${count}`);
      el.style.aspectRatio = `${width} / ${height}`;
      el.dataset['page'] = String(i);
      fragment.append(el);
      this.slots.push({
        pageNumber: i,
        el,
        drawnWidth: null,
        pendingWidth: null,
        task: null,
        textLayer: null,
        visible: i === 1,
      });
    }
    host.replaceChildren(fragment);
  }

  /** Draw (or redraw) the pages near the visible part of the pane, and
   *  give back the canvases of pages that scrolled well away. */
  private observeVisibility(seq: number): void {
    if (typeof IntersectionObserver === 'undefined') {
      // No observer (tests, very old engines): draw every page once.
      for (const slot of this.slots) {
        slot.visible = true;
        void this.drawPage(slot, seq);
      }
      return;
    }
    const bySlot = new Map(this.slots.map((s) => [s.el, s]));
    this.visibility = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const slot = bySlot.get(entry.target as HTMLDivElement);
          if (!slot) continue;
          slot.visible = entry.isIntersecting;
          if (entry.isIntersecting) void this.drawPage(slot, seq);
          else this.release(slot);
        }
      },
      { root: this.scroller().nativeElement, rootMargin: RENDER_MARGIN },
    );
    for (const slot of this.slots) this.visibility.observe(slot.el);
  }

  private async drawPage(slot: PageSlot, seq: number): Promise<void> {
    const doc = this.doc;
    const pdfjs = this.pdfjs;
    if (!doc || !pdfjs) return;

    const width = this.lastFitWidth;
    if (width <= 0) return;
    if (slot.drawnWidth === width || slot.pendingWidth === width) return;
    slot.pendingWidth = width;
    slot.task?.cancel();

    try {
      const page = await doc.getPage(slot.pageNumber);
      if (seq !== this.renderSeq || slot.pendingWidth !== width) return;

      const base = page.getViewport({ scale: 1 });
      const scale = width / base.width;
      const viewport = page.getViewport({ scale });
      const ratio = Math.min(globalThis.devicePixelRatio || 1, MAX_PIXEL_RATIO);

      // Drawn off to the side and swapped in once finished, so a redraw
      // never shows a blank page while it paints.
      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.setAttribute('aria-hidden', 'true');

      const task = page.render({
        canvas,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
      });
      slot.task = task;
      await task.promise;
      if (seq !== this.renderSeq || slot.pendingWidth !== width) return;

      const text = document.createElement('div');
      text.className = 'textLayer';
      const textLayer = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container: text,
        viewport,
      });
      await textLayer.render();
      if (seq !== this.renderSeq || slot.pendingWidth !== width) {
        textLayer.cancel();
        return;
      }

      slot.textLayer?.cancel();
      slot.textLayer = textLayer;
      slot.task = null;
      slot.el.style.aspectRatio = `${base.width} / ${base.height}`;
      slot.el.style.setProperty('--total-scale-factor', String(scale));
      slot.el.replaceChildren(canvas, text);
      slot.drawnWidth = width;
      slot.pendingWidth = null;

      // Scrolled away while it was drawing: give the canvas straight back.
      if (!slot.visible) this.release(slot);
    } catch (error) {
      if (slot.pendingWidth === width) slot.pendingWidth = null;
      // A cancelled draw (resized, scrolled away, closed) is expected.
      if (isCancellation(error)) return;
      // One unreadable page must not take the document down with it: the
      // slot stays empty and the rest still draw. The first page is the
      // exception, and `open` reports it.
      if (slot.pageNumber === 1) throw error;
    }
  }

  private release(slot: PageSlot): void {
    slot.task?.cancel();
    slot.task = null;
    slot.pendingWidth = null;
    slot.textLayer?.cancel();
    slot.textLayer = null;
    if (slot.drawnWidth === null) return;
    slot.drawnWidth = null;
    // Zeroing the canvas frees its backing store at once rather than
    // whenever the element is collected.
    for (const canvas of Array.from(slot.el.querySelectorAll('canvas'))) {
      canvas.width = 0;
      canvas.height = 0;
    }
    slot.el.replaceChildren();
  }

  private scheduleResize(): void {
    if (this.resizeTimer) clearTimeout(this.resizeTimer);
    const seq = this.renderSeq;
    this.resizeTimer = setTimeout(() => {
      this.resizeTimer = null;
      if (seq !== this.renderSeq || !this.doc) return;
      const width = this.fitWidth();
      if (width <= 0 || Math.abs(width - this.lastFitWidth) < 1) return;
      this.lastFitWidth = width;
      for (const slot of this.slots) {
        if (slot.visible) void this.drawPage(slot, seq);
      }
    }, RESIZE_DEBOUNCE_MS);
  }

  /** Width of the pane's content box: what a page is fitted to. */
  private fitWidth(): number {
    const el = this.scroller().nativeElement;
    const styles = getComputedStyle(el);
    return Math.floor(
      el.clientWidth -
        parseFloat(styles.paddingLeft || '0') -
        parseFloat(styles.paddingRight || '0'),
    );
  }

  private teardown(host: HTMLElement | null): void {
    this.visibility?.disconnect();
    this.visibility = null;
    for (const slot of this.slots) {
      slot.task?.cancel();
      slot.textLayer?.cancel();
    }
    this.slots = [];
    this.pageCount.set(0);
    host?.replaceChildren();
    // Destroying the loading task also destroys its document and frees
    // the worker-side copy of the bytes.
    void this.loadingTask?.destroy();
    this.loadingTask = null;
    this.doc = null;
  }
}

function isCancellation(error: unknown): boolean {
  return (error as { name?: string } | null)?.name === 'RenderingCancelledException';
}

function failureMessage(error: unknown): string {
  const name = (error as { name?: string } | null)?.name;
  if (name === 'PasswordException') {
    return 'This PDF is password-protected, so it can’t be previewed here. Download it to open it.';
  }
  return 'This file couldn’t be read as a PDF. It may be damaged or incomplete.';
}
