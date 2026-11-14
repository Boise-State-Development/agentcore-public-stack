import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChatBubbleLeftRight } from '@ng-icons/heroicons/outline';
import {
  DocxModelNode,
  DocxReviewItem,
  authorIndex,
  commentParaIds,
  decorateReview,
  formatReviewDate,
  orderReviewItems,
  revisionVerb,
  tagRevisions,
  threadComments,
  type CommentExtension,
} from './docx-review';

/** CSS px per point (96dpi / 72pt-per-inch). */
const PX_PER_PT = 96 / 72;

/** Names of the CSS highlights the page's comment ranges are painted
 *  with (`::highlight()` rules below). The registry is document-global,
 *  so the names are specific to this viewer. */
const HIGHLIGHT_COMMENTS = 'docx-review-comment';
const HIGHLIGHT_ACTIVE = 'docx-review-active';

type ReviewFilter = 'all' | 'comments' | 'changes';

/** The slice of docx-preview's parsed `WordDocument` the review reads.
 *  The library types the whole document as `any`. */
interface ParsedDocx {
  documentPart?: { body?: DocxModelNode };
  commentsPart?: {
    comments?: DocxModelNode[];
    path?: string;
    _package?: { load(path: string): Promise<string | null> };
  };
  commentsExtendedPart?: { commentMap?: Record<string, CommentExtension> };
}

/** The highlight API, where the engine has it (Chrome 105, Safari 17.2,
 *  Firefox 140). Without it the commented words are simply not tinted;
 *  the markers and the list still work. */
function highlightRegistry(): HighlightRegistry | null {
  return typeof CSS !== 'undefined' && 'highlights' in CSS && typeof Highlight !== 'undefined'
    ? CSS.highlights
    : null;
}

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
 *
 * **Review.** A document with comments or tracked changes also gets a
 * review list (`docx-review.ts`): every comment thread with its replies
 * and resolved state, and every change with its author and date, in page
 * order, plus a numbered marker and a tint on the words each comment is
 * about. A list item and its place in the page select each other. Left
 * to the library, comments never reach the page (`docx-review.ts`).
 */
@Component({
  selector: 'app-docx-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroChatBubbleLeftRight })],
  host: { '[style.--docx-zoom]': 'zoom()' },
  template: `
    <div class="@container flex h-full flex-col">
      @if (items().length > 0) {
        <div
          class="flex shrink-0 items-center gap-3 border-b border-gray-200 bg-white px-4 py-2 dark:border-gray-700 dark:bg-gray-900"
        >
          <p class="min-w-0 flex-1 truncate text-xs text-gray-600 dark:text-gray-300">
            {{ summary() }}
          </p>
          <button
            type="button"
            class="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            [attr.aria-expanded]="panelOpen()"
            aria-controls="docx-review-panel"
            (click)="togglePanel()"
          >
            <ng-icon name="heroChatBubbleLeftRight" class="text-base" aria-hidden="true" />
            {{ panelOpen() ? 'Hide review' : 'Show review' }}
          </button>
        </div>
      }

      <div class="flex min-h-0 flex-1 flex-col @4xl:flex-row">
        <div
          #scroller
          class="min-h-0 min-w-0 flex-1 overflow-auto bg-gray-100 px-4 py-6 dark:bg-gray-950"
        >
          <!-- docx-preview writes its stylesheet here and the document into
               the container below. Both are populated imperatively, so they
               must be plain elements Angular does not also manage. -->
          <div #styleHost hidden></div>
          <div #docHost class="docx-host" (click)="onDocumentClick($event)"></div>
        </div>

        @if (items().length > 0 && panelOpen()) {
          <aside
            #panel
            id="docx-review-panel"
            aria-label="Comments and tracked changes"
            class="flex max-h-[45%] shrink-0 flex-col border-t border-gray-200 bg-white @4xl:max-h-none @4xl:w-80 @4xl:border-t-0 @4xl:border-l dark:border-gray-700 dark:bg-gray-900"
          >
            <div
              role="group"
              aria-label="Show"
              class="flex shrink-0 gap-1 border-b border-gray-200 px-3 py-2 dark:border-gray-700"
            >
              @for (option of filterOptions(); track option.value) {
                <button
                  type="button"
                  class="rounded-md px-2 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                  [class]="
                    filter() === option.value
                      ? 'bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900'
                      : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                  "
                  [attr.aria-pressed]="filter() === option.value"
                  [disabled]="option.count === 0"
                  (click)="filter.set(option.value)"
                >
                  {{ option.label }} ({{ option.count }})
                </button>
              }
            </div>

            <ul class="min-h-0 flex-1 space-y-2 overflow-auto p-3">
              @for (item of visibleItems(); track item.key) {
                <li>
                  <button
                    type="button"
                    class="block w-full rounded-xl border p-3 text-left text-sm/6 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                    [class]="
                      selectedKey() === item.key
                        ? 'border-primary-500 bg-gray-50 ring-1 ring-primary-500 dark:border-primary-400 dark:bg-gray-800 dark:ring-primary-400'
                        : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800'
                    "
                    [attr.aria-current]="selectedKey() === item.key ? 'true' : null"
                    [attr.data-review-key]="item.key"
                    (click)="select(item.key, 'document')"
                  >
                    @if (item.kind === 'comment') {
                      <span class="flex items-center gap-2">
                        <span class="docx-review-number" [attr.data-author]="authorSlot(item.thread.comment.author)">
                          {{ item.number }}
                        </span>
                        <span class="min-w-0 flex-1 truncate font-medium text-gray-900 dark:text-gray-100">
                          {{ item.thread.comment.author }}
                        </span>
                        @if (item.thread.resolved) {
                          <span
                            class="shrink-0 rounded-full bg-gray-100 px-2 text-xs font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                          >
                            Resolved
                          </span>
                        }
                      </span>
                      @if (dateOf(item.thread.comment.date); as when) {
                        <span class="block text-xs text-gray-600 dark:text-gray-400">{{ when }}</span>
                      }
                      <span
                        class="mt-1 block whitespace-pre-line text-gray-800 dark:text-gray-200"
                       
                      >
                        {{ item.thread.comment.text || '(No text)' }}
                      </span>

                      @for (reply of item.thread.replies; track reply.id) {
                        <span
                          class="mt-2 block border-l-2 border-gray-200 pl-3 dark:border-gray-700"
                         
                        >
                          <span class="flex items-center gap-2">
                            <span class="docx-review-avatar" [attr.data-author]="authorSlot(reply.author)" aria-hidden="true">
                              {{ initialsOf(reply.initials, reply.author) }}
                            </span>
                            <span class="min-w-0 flex-1 truncate font-medium text-gray-900 dark:text-gray-100">
                              {{ reply.author }}
                            </span>
                          </span>
                          @if (dateOf(reply.date); as when) {
                            <span class="block text-xs text-gray-600 dark:text-gray-400">{{ when }}</span>
                          }
                          <span class="block whitespace-pre-line text-gray-800 dark:text-gray-200">
                            {{ reply.text || '(No text)' }}
                          </span>
                        </span>
                      }
                    } @else {
                      <span class="flex items-center gap-2">
                        <span class="docx-review-avatar" [attr.data-author]="authorSlot(item.revision.author)" aria-hidden="true">
                          {{ initialsOf('', item.revision.author) }}
                        </span>
                        <span class="min-w-0 flex-1 truncate font-medium text-gray-900 dark:text-gray-100">
                          {{ item.revision.author }}
                        </span>
                        <span class="shrink-0 text-xs font-medium text-gray-600 dark:text-gray-300">
                          {{ verbOf(item.revision.kind) }}
                        </span>
                      </span>
                      @if (dateOf(item.revision.date); as when) {
                        <span class="block text-xs text-gray-600 dark:text-gray-400">{{ when }}</span>
                      }
                      <span class="mt-1 line-clamp-4 block break-words">
                        @if (item.revision.deleted) {
                          <del class="text-state-danger-700 dark:text-state-danger-300">{{ item.revision.deleted }}</del>
                        }
                        @if (item.revision.kind === 'replace') {
                          <span class="text-gray-600 dark:text-gray-400" aria-hidden="true"> → </span>
                          <span class="sr-only">replaced with</span>
                        }
                        @if (item.revision.inserted) {
                          <ins class="text-state-success-700 dark:text-state-success-300">{{ item.revision.inserted }}</ins>
                        }
                        @if (!item.revision.deleted && !item.revision.inserted) {
                          <span class="text-gray-500 italic dark:text-gray-400">Formatting or a paragraph break</span>
                        }
                      </span>
                    }
                  </button>
                </li>
              }
            </ul>
          </aside>
        }
      </div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;

      /* Author colours, shared by the page markup and the list. Each is
         at least 4.5:1 as text on the (always white) page, on the amber
         comment tints below, and behind the list's white initials. */
      --docx-author-0: #b91c1c;
      --docx-author-1: #1d4ed8;
      --docx-author-2: #166534;
      --docx-author-3: #7e22ce;
      --docx-author-4: #92400e;
      --docx-author-5: #115e59;
      --docx-author-6: #be185d;
    }

    :host [data-author='0'], :host ::ng-deep [data-author='0'] { --docx-author: var(--docx-author-0); }
    :host [data-author='1'], :host ::ng-deep [data-author='1'] { --docx-author: var(--docx-author-1); }
    :host [data-author='2'], :host ::ng-deep [data-author='2'] { --docx-author: var(--docx-author-2); }
    :host [data-author='3'], :host ::ng-deep [data-author='3'] { --docx-author: var(--docx-author-3); }
    :host [data-author='4'], :host ::ng-deep [data-author='4'] { --docx-author: var(--docx-author-4); }
    :host [data-author='5'], :host ::ng-deep [data-author='5'] { --docx-author: var(--docx-author-5); }
    :host [data-author='6'], :host ::ng-deep [data-author='6'] { --docx-author: var(--docx-author-6); }

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

    /* Tracked changes in their author's colour, the way Word's "All
       Markup" draws them. */
    :host ::ng-deep ins[data-revision],
    :host ::ng-deep del[data-revision] {
      color: var(--docx-author);
      cursor: pointer;
      text-decoration-thickness: 1px;
    }
    :host ::ng-deep ins[data-revision] { text-decoration-line: underline; }
    :host ::ng-deep del[data-revision] { text-decoration-line: line-through; }
    :host ::ng-deep .docx-review-selected {
      background: rgb(253 230 138);
      outline: 2px solid rgb(217 119 6);
      outline-offset: 1px;
      border-radius: 2px;
    }

    /* The words a comment is about. */
    :host ::ng-deep ::highlight(docx-review-comment) {
      background-color: rgb(254 243 199);
    }
    :host ::ng-deep ::highlight(docx-review-active) {
      background-color: rgb(253 230 138);
      text-decoration: underline 2px rgb(217 119 6);
    }

    /* A comment thread's marker in the page, numbered like its list item. */
    :host ::ng-deep .docx-review-marker {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 1.5em;
      height: 1.5em;
      margin: 0 0.15em;
      padding: 0 0.35em;
      border: 0;
      border-radius: 9999px;
      background: var(--docx-author);
      color: #fff;
      font: 600 9pt/1 system-ui, sans-serif;
      vertical-align: super;
      cursor: pointer;
    }
    :host ::ng-deep .docx-review-marker[data-resolved] {
      background: var(--color-gray-600);
    }
    :host ::ng-deep .docx-review-marker:focus-visible,
    :host ::ng-deep .docx-review-marker[aria-current='true'] {
      outline: 2px solid rgb(217 119 6);
      outline-offset: 2px;
    }

    .docx-review-number,
    .docx-review-avatar {
      display: inline-flex;
      flex-shrink: 0;
      align-items: center;
      justify-content: center;
      min-width: 1.5rem;
      height: 1.5rem;
      padding: 0 0.375rem;
      border-radius: 9999px;
      background: var(--docx-author);
      color: #fff;
      font-size: 0.6875rem;
      font-weight: 600;
      line-height: 1;
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
  private readonly panel = viewChild<ElementRef<HTMLElement>>('panel');

  /** Scale applied to the rendered pages so a full page fits the pane
   *  width. Capped at 1 — a page narrower than the pane is shown at its
   *  true size rather than blown up. */
  protected readonly zoom = signal(1);

  /** Comment threads and tracked changes, in page order. */
  protected readonly items = signal<readonly DocxReviewItem[]>([]);
  protected readonly panelOpen = signal(true);
  protected readonly filter = signal<ReviewFilter>('all');
  protected readonly selectedKey = signal<string | null>(null);

  private readonly injector = inject(Injector);

  /** Distinct people who commented or changed something. */
  private readonly reviewers = signal(0);

  private authors = new Map<string, number>();
  private anchors = new Map<string, Element>();
  private ranges = new Map<string, Range>();
  private resolvedThreads = new Set<string>();

  protected readonly commentCount = computed(
    () => this.items().filter((i) => i.kind === 'comment').length,
  );
  protected readonly changeCount = computed(
    () => this.items().filter((i) => i.kind === 'revision').length,
  );

  protected readonly summary = computed(() => {
    const parts: string[] = [];
    const comments = this.commentCount();
    const changes = this.changeCount();
    if (comments) parts.push(`${comments} ${comments === 1 ? 'comment' : 'comments'}`);
    if (changes) parts.push(`${changes} tracked ${changes === 1 ? 'change' : 'changes'}`);
    const authors = this.reviewers();
    if (authors) parts.push(`${authors} ${authors === 1 ? 'reviewer' : 'reviewers'}`);
    return parts.join(' · ');
  });

  protected readonly filterOptions = computed(() => [
    { value: 'all' as const, label: 'All', count: this.items().length },
    { value: 'comments' as const, label: 'Comments', count: this.commentCount() },
    { value: 'changes' as const, label: 'Changes', count: this.changeCount() },
  ]);

  protected readonly visibleItems = computed(() => {
    const filter = this.filter();
    if (filter === 'all') return this.items();
    const kind = filter === 'comments' ? 'comment' : 'revision';
    return this.items().filter((i) => i.kind === kind);
  });

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
    // available width changes without the document changing — and the
    // review list opening or closing beside the pages changes it too.
    // Guarded for SSR, where there is no ResizeObserver and no layout.
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
        this.resetReview();
        return;
      }

      void this.render(data, host, styles, seq);
    });

    inject(DestroyRef).onDestroy(() => this.clearHighlights());
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
    // The library appends to the elements it is handed asynchronously,
    // so pointing two overlapping renders at the live host interleaves
    // their output — a big document still parsing when the user opens a
    // second one would paint into it afterwards, and the sequence guard
    // below cannot undo DOM the library wrote on its own. Fresh
    // containers also keep the injected stylesheet from accumulating a
    // copy per render.
    const docTarget = document.createElement('div');
    const styleTarget = document.createElement('div');

    try {
      DocxViewerComponent.libraryPromise ??= import('docx-preview');
      const { parseAsync, renderDocument } = await DocxViewerComponent.libraryPromise;
      if (seq !== this.renderSeq) return;

      const options = {
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
      };

      // parseAsync + renderDocument is what renderAsync does, split so the
      // review can tag each change node between the two (docx-review.ts).
      const doc = (await parseAsync(data, options)) as ParsedDocx;
      if (seq !== this.renderSeq) return;
      const revisions = tagRevisions(doc.documentPart?.body);
      const threads = threadComments(
        doc.commentsPart?.comments,
        commentParaIds(await this.readCommentsXml(doc)),
        doc.commentsExtendedPart?.commentMap,
      );

      const nodes = await renderDocument(doc, options);
      if (seq !== this.renderSeq) return;
      for (const node of nodes) {
        (node.nodeName === 'STYLE' ? styleTarget : docTarget).appendChild(node);
      }

      applyTableConditionalClasses(docTarget);
      const authors = authorIndex(revisions, threads);
      const decorated = decorateReview(docTarget, revisions, threads, authors);
      styles.replaceChildren(styleTarget);
      host.replaceChildren(docTarget);

      this.authors = authors;
      this.reviewers.set(authors.size);
      this.anchors = decorated.anchors;
      this.ranges = decorated.ranges;
      this.resolvedThreads = new Set(threads.filter((t) => t.resolved).map((t) => t.id));
      this.selectedKey.set(null);
      this.filter.set('all');
      this.items.set(orderReviewItems(threads, revisions, decorated));
      // Open on every document that has something to review: docked
      // beside wide pages, or stacked under narrow ones, where it costs
      // height rather than shrinking the page.
      this.panelOpen.set(true);
      this.paintHighlights();

      const firstPage = docTarget.querySelector<HTMLElement>('section.docx');
      this.pageWidthPx = firstPage ? naturalPageWidthPx(firstPage) : 0;
      this.applyFit();

      this.rendered.emit();
    } catch {
      if (seq !== this.renderSeq) return;
      host.replaceChildren();
      styles.replaceChildren();
      this.resetReview();
      this.renderFailed.emit(
        "This file couldn't be read as a Word document. It may be corrupted or saved in an older format.",
      );
    } finally {
      if (seq === this.renderSeq) this.busy.set(false);
    }
  }

  /** `comments.xml` as text, for the paragraph ids replies are threaded
   *  by. Best effort: without it every comment is its own thread. */
  private async readCommentsXml(doc: ParsedDocx): Promise<string | null> {
    const part = doc.commentsPart;
    if (!part?.path || !part._package) return null;
    try {
      return await part._package.load(part.path);
    } catch {
      return null;
    }
  }

  protected togglePanel(): void {
    this.panelOpen.update((open) => !open);
  }

  protected authorSlot(author: string): number {
    return this.authors.get(author) ?? 0;
  }

  protected dateOf(date: string | null): string | null {
    return formatReviewDate(date);
  }

  protected verbOf(kind: Parameters<typeof revisionVerb>[0]): string {
    return revisionVerb(kind);
  }

  protected initialsOf(initials: string, author: string): string {
    if (initials) return initials.slice(0, 3).toUpperCase();
    const words = author.split(/\s+/).filter(Boolean);
    // Word's own rule when a comment carries no initials: first and last
    // name's first letters, or the one letter of a single-word name.
    return (words.length > 1 ? words[0][0] + words[words.length - 1][0] : author.slice(0, 1)).toUpperCase();
  }

  /** A click on a comment marker or a tracked change selects it in the list. */
  protected onDocumentClick(event: MouseEvent): void {
    const target = (event.target as Element | null)?.closest?.(
      '[data-comment-thread], [data-revision]',
    );
    if (!target) return;
    const thread = target.getAttribute('data-comment-thread');
    const key = thread !== null ? `c:${thread}` : `r:${target.getAttribute('data-revision')}`;
    if (!this.anchors.has(key)) return;
    this.panelOpen.set(true);
    if (this.filter() !== 'all' && this.filter() !== (thread !== null ? 'comments' : 'changes')) {
      this.filter.set('all');
    }
    this.select(key, 'list');
  }

  /**
   * Make `key` the selected item: mark it in the page and in the list, and
   * bring the *other* side into view — the page when a list item was
   * clicked, the list when the page was.
   */
  protected select(key: string, reveal: 'document' | 'list'): void {
    this.selectedKey.set(key);
    this.paintSelection(key);

    if (reveal === 'document') {
      const anchor = this.anchors.get(key);
      if (anchor) this.scrollIntoScroller(anchor);
      return;
    }
    // The list may have just opened, or been re-filtered; reveal the card
    // once that has rendered. Attributes are compared rather than built
    // into a selector, because a comment id is whatever the file says.
    afterNextRender(
      () => {
        const cards = this.panel()?.nativeElement.querySelectorAll<HTMLElement>('[data-review-key]');
        const card = Array.from(cards ?? []).find((c) => c.dataset['reviewKey'] === key);
        card?.scrollIntoView?.({ block: 'nearest', behavior: this.scrollBehavior() });
      },
      { injector: this.injector },
    );
  }

  private paintSelection(key: string | null): void {
    const root = this.docHost().nativeElement;
    for (const el of Array.from(root.querySelectorAll('.docx-review-selected'))) {
      el.classList.remove('docx-review-selected');
    }
    for (const el of Array.from(root.querySelectorAll('.docx-review-marker[aria-current]'))) {
      el.removeAttribute('aria-current');
    }
    if (key?.startsWith('r:')) {
      const id = key.slice(2);
      for (const el of Array.from(root.querySelectorAll('[data-revision]'))) {
        if (el.getAttribute('data-revision') === id) el.classList.add('docx-review-selected');
      }
    } else if (key?.startsWith('c:')) {
      this.anchors.get(key)?.setAttribute('aria-current', 'true');
    }
    this.paintHighlights();
  }

  /** Tint every open thread's words, and the selected thread's (resolved
   *  or not) more strongly. */
  private paintHighlights(): void {
    const registry = highlightRegistry();
    if (!registry) return;
    const selected = this.selectedKey();
    const open = Array.from(this.ranges)
      .filter(([id]) => !this.resolvedThreads.has(id))
      .map(([, range]) => range);
    registry.set(HIGHLIGHT_COMMENTS, new Highlight(...open));
    const active = selected?.startsWith('c:') ? this.ranges.get(selected.slice(2)) : undefined;
    if (active) registry.set(HIGHLIGHT_ACTIVE, new Highlight(active));
    else registry.delete(HIGHLIGHT_ACTIVE);
  }

  private clearHighlights(): void {
    const registry = highlightRegistry();
    registry?.delete(HIGHLIGHT_COMMENTS);
    registry?.delete(HIGHLIGHT_ACTIVE);
  }

  private resetReview(): void {
    this.items.set([]);
    this.selectedKey.set(null);
    this.authors = new Map();
    this.reviewers.set(0);
    this.anchors = new Map();
    this.ranges = new Map();
    this.resolvedThreads = new Set();
    this.clearHighlights();
  }

  /** Centre `el` in the page scroller. Done by hand, not with
   *  `scrollIntoView`, which would also scroll every ancestor — the chat
   *  behind the pane included. */
  private scrollIntoScroller(el: Element): void {
    const scroller = this.scroller().nativeElement;
    const box = scroller.getBoundingClientRect();
    const target = el.getBoundingClientRect();
    scroller.scrollBy?.({
      top: target.top - box.top - box.height / 2 + target.height / 2,
      left: target.left < box.left || target.right > box.right ? target.left - box.left - 16 : 0,
      behavior: this.scrollBehavior(),
    });
  }

  private scrollBehavior(): ScrollBehavior {
    return typeof matchMedia === 'function' &&
      matchMedia('(prefers-reduced-motion: reduce)').matches
      ? 'auto'
      : 'smooth';
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
