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

/** CSS px per point (96dpi / 72pt-per-inch). */
const PX_PER_PT = 96 / 72;

/**
 * Natural width of a rendered page, in CSS px.
 *
 * Read from the inline `width` docx-preview stamps on each `section.docx`
 * (`width: 612pt` for US Letter) rather than measured from layout,
 * because a measurement taken while a zoom is already applied reports the
 * scaled box in some engines — which would make the fit computation feed
 * on its own output and drift on every resize.
 */
function naturalPageWidthPx(page: HTMLElement): number {
  const raw = page.style.width;
  const value = parseFloat(raw);
  if (!Number.isFinite(value) || value <= 0) return 0;
  return raw.trim().endsWith('pt') ? value * PX_PER_PT : value;
}


/**
 * Re-attach the per-row / per-cell classes docx-preview's own stylesheet
 * needs, which docx-preview 0.4.0 never emits.
 *
 * Word tables carry a `tblLook` saying which conditional bands of the
 * table style are switched on (header row, first column, row/column
 * banding). The library renders those flags onto the `<table>` itself
 * (`class="first-row first-col no-vband docx_lightgrid-accent1"`) and
 * emits matching CSS — but the CSS is written against the *rows and
 * cells* (`tr.first-row td span` for the bold header, `tr.odd-row` for
 * the band fill), and no `<tr>` or `<td>` ever receives those classes.
 * The rules can therefore never match, and a table that Word draws with
 * a bold header and banded rows renders flat.
 *
 * This invents no formatting of its own: it only tags the elements the
 * library's own per-style rules already target, so each style decides
 * what its own header and bands look like, and a style with no rule for
 * a given band matches nothing and changes nothing.
 *
 * Band numbering is 1-based over the *body* rows/columns — the header
 * and total rows are excluded when the table says they are special, and
 * the first body row is an odd band. That is what Word draws: with a
 * header row plus five data rows, data rows 1, 3 and 5 take the fill.
 */
function applyTableConditionalClasses(root: HTMLElement): void {
  for (const table of Array.from(root.querySelectorAll('table'))) {
    const flags = table.classList;
    const rows = Array.from(table.rows);
    if (rows.length === 0) continue;

    const hasFirstRow = flags.contains('first-row');
    const hasLastRow = flags.contains('last-row');
    const hasFirstCol = flags.contains('first-col');
    const hasLastCol = flags.contains('last-col');

    if (hasFirstRow) rows[0].classList.add('first-row');
    if (hasLastRow) rows[rows.length - 1].classList.add('last-row');

    // Rows that take part in banding: everything the style has not
    // already claimed as a header or total row.
    const bodyRows = rows.slice(
      hasFirstRow ? 1 : 0,
      hasLastRow ? rows.length - 1 : rows.length,
    );
    if (!flags.contains('no-hband')) {
      bodyRows.forEach((row, i) => {
        row.classList.add(i % 2 === 0 ? 'odd-row' : 'even-row');
      });
    }

    const vband = !flags.contains('no-vband');
    if (!hasFirstCol && !hasLastCol && !vband) continue;

    for (const row of rows) {
      const cells = Array.from(row.cells);
      if (cells.length === 0) continue;

      if (hasFirstCol) cells[0].classList.add('first-col');
      if (hasLastCol) cells[cells.length - 1].classList.add('last-col');

      if (!vband) continue;
      // Only `odd-col` exists in the emitted stylesheet — the even band
      // is the table's default cell styling, so it needs no class.
      const bodyCells = cells.slice(
        hasFirstCol ? 1 : 0,
        hasLastCol ? cells.length - 1 : cells.length,
      );
      bodyCells.forEach((cell, i) => {
        if (i % 2 === 0) cell.classList.add('odd-col');
      });
    }
  }
}

/**
 * Renders a `.docx` in the browser, from its raw bytes.
 *
 * Uses `docx-preview`, which walks the OOXML and reproduces the
 * document's own page layout, styles, tables and numbering as DOM. The
 * alternative in this space, `mammoth`, deliberately flattens to
 * "simple HTML" and discards styling — right for a docx -> Markdown
 * conversion, wrong for something the user opened expecting to see
 * their document.
 *
 * Nothing leaves the browser. This is the whole reason the pane exists
 * rather than the reference implementation's iframe onto
 * `view.officeapps.live.com`, which renders server-side at Microsoft
 * and therefore needs the document reachable by an unauthenticated URL.
 *
 * The library is loaded with a dynamic `import()`, for two reasons: it
 * and its `jszip` dependency are dead weight in the initial bundle for
 * the large majority of sessions that never open a Word document, and
 * it touches `document` at module scope, so a static import would run
 * during SSR.
 */
@Component({
  selector: 'app-docx-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '[style.--docx-zoom]': 'zoom()' },
  template: `
    <div
      #scroller
      class="h-full overflow-auto bg-gray-100 px-4 py-6 dark:bg-gray-950"
    >
      <!-- docx-preview writes its stylesheet here and the document into
           the container below. Both are populated imperatively, so they
           must be plain elements Angular does not also manage. -->
      <div #styleHost hidden></div>
      <div #docHost class="docx-host"></div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }

    /* docx-preview builds this subtree imperatively, so it never
       receives Angular's emulated-encapsulation attribute and cannot be
       reached by an ordinary rule here. ::ng-deep under :host is the
       supported escape hatch and keeps the selector anchored to this
       component's element. */
    :host ::ng-deep .docx-wrapper {
      background: transparent;
      padding: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.5rem;
      /* Scale the whole page down so a US-Letter sheet fits the pane.
         CSS zoom rather than transform: scale() on purpose: zoom
         participates in layout, so the flow collapses to the scaled
         height on its own. A transform would paint smaller while still
         reserving the unscaled height, leaving a growing band of dead
         space under every page that has to be clawed back with negative
         margins. */
      zoom: var(--docx-zoom, 1);
    }

    /* Each rendered page. Left white in both themes on purpose — this is
       a preview of a printable document, and recolouring the page would
       misrepresent what the user would get out of Word. The surrounding
       gutter carries the theme instead.

       The max-width below is belt-and-braces: the zoom above should
       already make the page fit, so it only bites in the frame before
       the fit calculation has run. */
    :host ::ng-deep .docx-wrapper > section.docx {
      background: var(--color-white);
      box-shadow: 0 1px 3px rgb(0 0 0 / 0.12), 0 1px 2px rgb(0 0 0 / 0.08);
      border-radius: 0.125rem;
      max-width: 100%;
    }
  `,
})
export class DocxViewerComponent {
  /** Raw `.docx` bytes. Null clears the view (pane closed / still loading). */
  readonly bytes = input<ArrayBuffer | null>(null);

  /** The document painted. */
  readonly rendered = output<void>();

  /** Rendering failed — the bytes were not a readable `.docx`. */
  readonly renderFailed = output<string>();

  private readonly docHost =
    viewChild.required<ElementRef<HTMLElement>>('docHost');
  private readonly styleHost =
    viewChild.required<ElementRef<HTMLElement>>('styleHost');
  private readonly scroller =
    viewChild.required<ElementRef<HTMLElement>>('scroller');

  /** Scale applied to the rendered pages so a full page fits the pane
   *  width. Capped at 1 — a page narrower than the pane is shown at its
   *  true size rather than blown up. */
  protected readonly zoom = signal(1);

  /** Natural width of one page in CSS px, from the last render. Held so
   *  a resize can re-fit without re-parsing the document. */
  private pageWidthPx = 0;

  /** Bumped per render so a slow parse that resolves after the input
   *  changed (or the pane closed) cannot paint over the current
   *  document. */
  private renderSeq = 0;

  /** Cached module so reopening the pane doesn't re-request the chunk. */
  private static libraryPromise: Promise<
    typeof import('docx-preview')
  > | null = null;

  protected readonly busy = signal(false);

  constructor() {
    // Re-fit when the pane is resized. The rail is draggable, so the
    // available width changes without the document changing. Guarded for
    // SSR, where there is no ResizeObserver and no layout to fit to.
    effect((onCleanup) => {
      if (typeof ResizeObserver === 'undefined') return;
      const ro = new ResizeObserver(() => this.applyFit());
      ro.observe(this.scroller().nativeElement);
      onCleanup(() => ro.disconnect());
    });

    effect(() => {
      const data = this.bytes();
      const host = this.docHost().nativeElement;
      const styles = this.styleHost().nativeElement;

      const seq = ++this.renderSeq;

      if (!data) {
        host.replaceChildren();
        styles.replaceChildren();
        this.pageWidthPx = 0;
        this.zoom.set(1);
        return;
      }

      void this.render(data, host, styles, seq);
    });
  }

  private async render(
    data: ArrayBuffer,
    host: HTMLElement,
    styles: HTMLElement,
    seq: number,
  ): Promise<void> {
    this.busy.set(true);

    // Render into detached containers and swap them in only on success.
    //
    // `renderAsync` captures the elements it is handed and appends to
    // them asynchronously, so pointing two overlapping renders at the
    // live host interleaves their output — a big document still parsing
    // when the user opens a second one would paint into it afterwards,
    // and the sequence guard below cannot undo DOM the library wrote on
    // its own. Fresh containers also keep the injected stylesheet from
    // accumulating a copy per render.
    const docTarget = document.createElement('div');
    const styleTarget = document.createElement('div');

    try {
      DocxViewerComponent.libraryPromise ??= import('docx-preview');
      const { renderAsync } = await DocxViewerComponent.libraryPromise;
      if (seq !== this.renderSeq) return;

      await renderAsync(data, docTarget, styleTarget, {
        // Keep the page frames: the point of this renderer over a
        // to-HTML converter is that the preview looks like the document.
        inWrapper: true,
        breakPages: true,
        ignoreLastRenderedPageBreak: false,
        // Honour the document's own page size. Width is capped by our
        // CSS instead, so a landscape or wide-margin document doesn't
        // force the pane into horizontal scroll at narrow widths.
        //
        // `ignoreHeight` must stay false: it is what makes the renderer
        // emit `min-height: <page height>` on each page (1056px for
        // US Letter at 96dpi). With it true a short document collapses
        // to the height of its own text and stops reading as a page at
        // all — which is the entire point of using this renderer over a
        // to-HTML converter.
        ignoreWidth: false,
        ignoreHeight: false,
        // Tracked changes and comments are part of what a reviewer came
        // to see; a document without them renders identically.
        renderChanges: true,
        renderComments: true,
        renderHeaders: true,
        renderFooters: true,
        renderFootnotes: true,
        renderEndnotes: true,
        // Inline embedded images as data URLs rather than blob: URLs.
        // Blob URLs are revoked with the document object and would leave
        // broken images behind when the pane re-renders.
        useBase64URL: true,
      });

      if (seq !== this.renderSeq) return;
      applyTableConditionalClasses(docTarget);
      styles.replaceChildren(styleTarget);
      host.replaceChildren(docTarget);

      const firstPage = docTarget.querySelector<HTMLElement>('section.docx');
      this.pageWidthPx = firstPage ? naturalPageWidthPx(firstPage) : 0;
      this.applyFit();

      this.rendered.emit();
    } catch {
      if (seq !== this.renderSeq) return;
      host.replaceChildren();
      styles.replaceChildren();
      this.renderFailed.emit(
        "This file couldn't be read as a Word document. It may be corrupted or saved in an older format.",
      );
    } finally {
      if (seq === this.renderSeq) this.busy.set(false);
    }
  }

  /**
   * Scale the pages so one full page fits the pane's content box.
   *
   * No-op until a render has recorded a page width, and never scales
   * above 1: once the user widens the rail past a full page there is
   * nothing to gain from enlarging it, and blowing a document up past
   * its true size would misrepresent it.
   */
  private applyFit(): void {
    const el = this.scroller().nativeElement;
    const styles = getComputedStyle(el);
    const available =
      el.clientWidth -
      parseFloat(styles.paddingLeft || '0') -
      parseFloat(styles.paddingRight || '0');

    if (this.pageWidthPx <= 0 || available <= 0) {
      this.zoom.set(1);
      return;
    }
    this.zoom.set(Math.min(1, available / this.pageWidthPx));
  }
}
