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
  untracked,
  viewChild,
} from '@angular/core';
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  RenderTask,
  TextLayer,
} from 'pdfjs-dist';
import { PdfJsLoader, type PdfJs } from './pdfjs-loader';
import { findQuote } from './pdf-quote-match';

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

/** How long a page someone jumped to stays outlined. */
const TARGET_HIGHLIGHT_MS = 1600;

/**
 * A page to show. An object rather than a bare number so asking for the
 * same page twice is still a change: a reader who scrolled away and
 * clicks the same citation again expects to be taken back.
 */
export interface PdfPageTarget {
  /** 1-based, as pdf.js and `source_provenance` locators number pages. */
  page: number;
  /** A passage on that page to highlight and bring into view. */
  quote?: string;
}

/** Class on the `<mark>` elements that highlight a quoted passage. */
const QUOTE_MARK_CLASS = 'pdf-quote';

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
 * A `targetPage` (set when a citation names a page) scrolls to that page,
 * outlines it briefly and moves focus to it, so a screen reader announces
 * where the reader landed. Given before the document has opened, it is
 * where the document opens: that page is drawn first, not page 1. With a
 * `quote`, the passage is found in the page's text layer (`findQuote`),
 * wrapped in `<mark>` and scrolled into view. The highlight stays until
 * another page is asked for, and is put back whenever the page is redrawn
 * (a resize, or scrolling away and back). A quote the text layer does not
 * contain leaves the page outlined and nothing marked: a highlight on the
 * wrong words would be worse than none.
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

    /* The page a citation jumped to. An outline rather than a border so
       nothing reflows, and no animation, so reduced motion needs no case. */
    :host ::ng-deep .pdf-page.pdf-page-target {
      outline: 3px solid var(--color-primary-500);
      outline-offset: 3px;
    }

    /* The quoted passage. The text layer's glyphs are transparent over the
       canvas, so the mark is only a tint behind the drawn text. */
    :host ::ng-deep .textLayer mark.pdf-quote {
      background: rgb(250 204 21 / 0.45);
      color: transparent;
      border-radius: 2px;
      padding: 0;
    }

    @media (forced-colors: active) {
      :host ::ng-deep .textLayer mark.pdf-quote {
        background: Mark;
        forced-color-adjust: none;
      }
    }

    :host ::ng-deep .pdf-page:focus {
      outline: none;
    }

    :host ::ng-deep .pdf-page:focus-visible,
    :host ::ng-deep .pdf-page.pdf-page-target:focus {
      outline: 3px solid var(--color-primary-500);
      outline-offset: 3px;
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

  /** A page to show. Ignored when it is not a page the document has. */
  readonly targetPage = input<PdfPageTarget | null>(null);

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
  private highlightTimer: ReturnType<typeof setTimeout> | null = null;
  /** The passage to highlight, and on which page. */
  private quoteTarget: { page: number; quote: string } | null = null;
  /** Bring the passage into view the next time it is marked: once per jump,
   *  never again on a redraw the reader did not ask for. */
  private scrollToQuote = false;

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

    // A page asked for once the document is showing. Before that, `open`
    // reads the target itself and starts there.
    effect(() => {
      const target = this.targetPage();
      if (!target || !this.doc || !this.slots.length) return;
      untracked(() => this.goToPage(target.page, target.quote));
    });

    inject(DestroyRef).onDestroy(() => {
      this.renderSeq++;
      if (this.resizeTimer) clearTimeout(this.resizeTimer);
      if (this.highlightTimer) clearTimeout(this.highlightTimer);
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

      // Size every slot from the first page, then draw the page the reader
      // will see first — page 1, or the one a citation asked for — straight
      // away: the panel's skeleton stays up until it is on screen.
      const first = await doc.getPage(1);
      if (seq !== this.renderSeq) return;
      const base = first.getViewport({ scale: 1 });
      this.buildSlots(host, doc.numPages, base.width, base.height);
      this.pageCount.set(doc.numPages);

      const wanted = untracked(this.targetPage);
      const target = this.pageIndex(wanted?.page);
      if (target !== null) this.setQuoteTarget(target + 1, wanted?.quote);
      const start = this.slots[target ?? 0];
      start.visible = true;
      this.lastFitWidth = this.fitWidth();
      if (target !== null) this.scrollToSlot(start);
      await this.drawPage(start, seq);
      if (seq !== this.renderSeq) return;

      this.observeVisibility(seq);
      this.rendered.emit();
      // Again once drawn: the page may have corrected its slot's size.
      if (target !== null) this.goToPage(target + 1, wanted?.quote);
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
      // Focusable from script only, so a jump can announce where it landed.
      el.tabIndex = -1;
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
        visible: false,
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
      if (this.quoteTarget?.page === slot.pageNumber) this.markQuote(slot);

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

  /** Slot index for a 1-based page number, or null if there is no such page. */
  private pageIndex(page: number | undefined): number | null {
    if (!page || !Number.isInteger(page)) return null;
    if (page < 1 || page > this.slots.length) return null;
    return page - 1;
  }

  /**
   * Take the reader to a page: scroll it to the top of the pane, outline
   * it briefly, and move focus to it so assistive tech says which page
   * this is. The visibility observer draws it once it is in view.
   */
  private goToPage(page: number, quote?: string): void {
    const index = this.pageIndex(page);
    if (index === null) return;
    const slot = this.slots[index];
    this.setQuoteTarget(page, quote);
    this.scrollToSlot(slot);
    // Already drawn: mark it now. Otherwise `drawPage` marks it once the
    // visibility observer has it drawn.
    if (slot.drawnWidth !== null) this.markQuote(slot);

    for (const other of this.slots) other.el.classList.remove('pdf-page-target');
    slot.el.classList.add('pdf-page-target');
    if (this.highlightTimer) clearTimeout(this.highlightTimer);
    this.highlightTimer = setTimeout(() => {
      this.highlightTimer = null;
      slot.el.classList.remove('pdf-page-target');
    }, TARGET_HIGHLIGHT_MS);

    slot.el.focus({ preventScroll: true });
  }

  /** Replace the passage to highlight, clearing any marks from the last. */
  private setQuoteTarget(page: number, quote: string | undefined): void {
    this.clearQuoteMarks();
    const text = quote?.trim();
    this.quoteTarget = text ? { page, quote: text } : null;
    this.scrollToQuote = !!text;
  }

  /**
   * Highlight the quoted passage in a drawn page's text layer, and bring it
   * into view if this is the jump that asked for it.
   */
  private markQuote(slot: PageSlot): void {
    const target = this.quoteTarget;
    const layer = slot.el.querySelector<HTMLElement>('.textLayer');
    if (!target || !layer || layer.querySelector(`mark.${QUOTE_MARK_CLASS}`)) return;

    const nodes: Text[] = [];
    const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text);

    const ranges = findQuote(
      nodes.map((n) => n.data),
      target.quote,
    );
    if (!ranges.length) return;

    // Last range first, so splitting a node never shifts the offsets of a
    // range still to come in that same node.
    const marks: HTMLElement[] = [];
    for (const range of [...ranges].reverse()) {
      const node = nodes[range.text];
      if (range.end < node.data.length) node.splitText(range.end);
      const middle = range.start > 0 ? node.splitText(range.start) : node;
      const mark = document.createElement('mark');
      mark.className = QUOTE_MARK_CLASS;
      middle.replaceWith(mark);
      mark.append(middle);
      marks.unshift(mark);
    }

    if (this.scrollToQuote) {
      this.scrollToQuote = false;
      this.bringIntoView(marks[0]);
    }
  }

  /**
   * Scroll so `el` sits a quarter of the way down the pane, unless it is
   * already comfortably in view — a passage near the top of the page is
   * best read with the page's top edge still showing.
   */
  private bringIntoView(el: HTMLElement): void {
    const scroller = this.scroller().nativeElement;
    const box = scroller.getBoundingClientRect();
    const top = el.getBoundingClientRect().top - box.top;
    if (top >= 0 && top <= scroller.clientHeight * 0.6) return;
    scroller.scrollTop += top - scroller.clientHeight * 0.25;
  }

  private clearQuoteMarks(): void {
    const host = this.pagesHost().nativeElement;
    for (const mark of Array.from(host.querySelectorAll(`mark.${QUOTE_MARK_CLASS}`))) {
      const parent = mark.parentNode;
      mark.replaceWith(...Array.from(mark.childNodes));
      parent?.normalize();
    }
  }

  private scrollToSlot(slot: PageSlot): void {
    const scroller = this.scroller().nativeElement;
    const paddingTop = parseFloat(getComputedStyle(scroller).paddingTop || '0');
    const offset =
      slot.el.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    scroller.scrollTop += offset - paddingTop;
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
    this.quoteTarget = null;
    this.scrollToQuote = false;
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
