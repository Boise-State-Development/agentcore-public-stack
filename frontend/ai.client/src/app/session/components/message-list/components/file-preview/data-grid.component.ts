import {
  CdkVirtualScrollViewport,
  ScrollingModule,
} from '@angular/cdk/scrolling';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  viewChild,
} from '@angular/core';

/**
 * Height of one body row, in CSS px.
 *
 * Load-bearing: `cdk-virtual-scroll-viewport`'s fixed-size strategy
 * computes the spacer height and the rendered window from this number,
 * so a row that paints taller than it claims leaves the rows drifting
 * out of the viewport as you scroll. Keep it in step with the row height
 * in the styles below.
 */
const ROW_HEIGHT_PX = 32;

/** Column width bounds, in CSS px. */
const COL_MIN_PX = 88;
const COL_MAX_PX = 320;

/**
 * Approximate px per character at the grid's text size, for sizing
 * columns without laying anything out first.
 *
 * Measured against the real font at 13px: mixed-case words average
 * about 6.7px, dates 7.3, digits 7.8, and formula text like
 * "=SUM(B2:C2)" close to 8.1 — the wide glyphs are exactly the ones
 * spreadsheet content is full of. An estimate tuned to the average
 * clipped those by a few px, which reads as a bug rather than as the
 * deliberate ellipsis at COL_MAX_PX, so this sits at the top of the
 * range instead. Over-estimating only costs a slightly wider column.
 */
const PX_PER_CHAR = 8.2;

/** Rows sampled when measuring a column's natural width. Measuring all
 *  of them would walk 50k rows to move a column by a few px. */
const WIDTH_SAMPLE_ROWS = 100;

/**
 * A scrollable grid of strings — the shared body of every data preview.
 *
 * Purely presentational: it takes headers and rows and draws them. Where
 * those came from is the caller's business, which is what lets one grid
 * serve both a `.csv` parsed in the browser and an `.xlsx` read by
 * app-api. Cells are rendered exactly as handed over, with no type
 * inference, number formatting or date parsing — a preview that
 * prettifies `007` into `7` is answering a different question than the
 * one being asked, and that has to stay true no matter which reader
 * produced the strings.
 *
 * Virtualised with `cdk-virtual-scroll-viewport`. `@angular/cdk` is
 * already a dependency (dialog, menu, overlay, a11y), uniform row height
 * is the fixed-size strategy's best case, and the alternative —
 * "showing the first 500 rows of 40,000" — is the difference between
 * previewing a file and previewing the top of one.
 *
 * The cost of virtualising is that the grid cannot be a `<table>`: the
 * viewport needs to own the scroll container and transform its content,
 * which `<tbody>` will not tolerate. So it is a div grid carrying
 * explicit ARIA grid roles, with `aria-rowcount` and `aria-rowindex`
 * reporting the true size of the data rather than the size of the
 * rendered window.
 */
@Component({
  selector: 'app-data-grid',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ScrollingModule],
  template: `
    <div
      class="flex h-full flex-col bg-white dark:bg-gray-900"
      role="grid"
      [attr.aria-rowcount]="rows().length + 1"
      [attr.aria-colcount]="headers().length"
      [attr.aria-label]="ariaLabel()"
      [style.--grid-cols]="gridTemplate()"
    >
      <!--
        The header sits outside the viewport because a sticky row inside
        a transformed virtual-scroll container does not stay put. Its
        horizontal offset is mirrored from the viewport's scroll instead
        — one-way, since the header itself does not scroll.
      -->
      <div
        #headerScroller
        class="overflow-hidden border-b border-gray-200 dark:border-gray-700"
      >
        <div class="grid-row bg-gray-50 dark:bg-gray-800" role="row" aria-rowindex="1">
          <div
            class="grid-gutter text-gray-500 dark:text-gray-400"
            role="columnheader"
            aria-label="Row number"
          >
            <span aria-hidden="true">#</span>
          </div>
          @for (header of headers(); track $index) {
            <div
              class="grid-cell font-semibold text-gray-900 dark:text-gray-100"
              role="columnheader"
              [attr.aria-colindex]="$index + 2"
              [title]="header"
            >
              {{ header }}
            </div>
          }
        </div>
      </div>

      @if (rows().length === 0) {
        <p class="px-4 py-6 text-sm text-gray-500 dark:text-gray-400">
          {{ emptyMessage() }}
        </p>
      } @else {
        <cdk-virtual-scroll-viewport [itemSize]="rowHeight" class="min-h-0 flex-1">
          <div
            *cdkVirtualFor="let row of rows(); let i = index; trackBy: trackByIndex"
            class="grid-row border-b border-gray-100 hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-800/50"
            role="row"
            [attr.aria-rowindex]="i + 2"
          >
            <div
              class="grid-gutter text-gray-500 dark:text-gray-400"
              role="rowheader"
            >
              {{ i + 1 }}
            </div>
            @for (cell of row; track $index) {
              <div
                class="grid-cell text-gray-700 dark:text-gray-300"
                role="gridcell"
                [attr.aria-colindex]="$index + 2"
                [title]="cell"
              >
                {{ cell }}
              </div>
            }
          </div>
        </cdk-virtual-scroll-viewport>
      }

      <footer
        class="flex items-center gap-2 border-t border-gray-200 px-4 py-2 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400"
      >
        <span>{{ summary() }}</span>
        @if (notice()) {
          <!--
            -700 rather than -600 in light mode: measured against the
            grid's white surface, -600 is 3.2:1 and fails WCAG AA for
            normal-size text, while -700 clears it at 5.03:1. The dark
            step stays at -400 (10.3:1) — -700 would be 3.53:1 there.
          -->
          <span class="text-state-warning-700 dark:text-state-warning-400">
            {{ notice() }}
          </span>
        }
      </footer>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }

    /* Header and body rows share one track list so the columns line up
       without measuring anything after layout. */
    .grid-row {
      display: grid;
      grid-template-columns: var(--grid-cols);
      align-items: center;
      height: 32px;
      width: max-content;
      min-width: 100%;
    }

    .grid-cell,
    .grid-gutter {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      padding-inline: 0.75rem;
      font-size: 0.8125rem;
      line-height: 1.25rem;
      font-variant-numeric: tabular-nums;
    }

    /* Colour deliberately lives on the elements, as the utility pair
       text-gray-500 / dark:text-gray-400, rather than here. There is no
       single neutral step that clears WCAG AA against both surfaces:
       gray-400 measures 2.6:1 on the light grid, and gray-500 is the
       step the surface generator itself reports as short of AA against
       the dark one. (No backticks in this block - one inside a styles
       template literal breaks the Angular compiler while tsc passes.) */
    .grid-gutter {
      text-align: right;
      padding-inline: 0.5rem;
      font-size: 0.6875rem;
      user-select: none;
    }

    /* The viewport is the horizontal scroller as well as the vertical
       one, so rows wider than the rail can be reached. */
    cdk-virtual-scroll-viewport {
      overflow-x: auto;
    }
  `,
})
export class DataGridComponent {
  readonly headers = input.required<readonly string[]>();
  readonly rows = input.required<readonly (readonly string[])[]>();
  /** Footer line: size, and whatever else the reader knows. */
  readonly summary = input('');
  /** Footer warning, shown after the summary when a cap cut the data. */
  readonly notice = input('');
  /** Shown in place of the body when there are headers but no rows. */
  readonly emptyMessage = input('No data rows to show.');
  readonly ariaLabel = input('Data preview');

  protected readonly rowHeight = ROW_HEIGHT_PX;

  private readonly headerScroller =
    viewChild<ElementRef<HTMLElement>>('headerScroller');
  private readonly viewport = viewChild(CdkVirtualScrollViewport);

  /** One `grid-template-columns` track list, shared by the header row
   *  and every body row. */
  protected readonly gridTemplate = computed(() => {
    const headers = this.headers();
    const rows = this.rows();
    const gutter = `${gutterWidthFor(rows.length)}px`;
    const columns = headers.map(
      (header, i) => `${measureColumn(header, rows, i)}px`,
    );
    return [gutter, ...columns].join(' ');
  });

  constructor() {
    effect((onCleanup) => {
      const viewport = this.viewport();
      if (!viewport) return;
      const sub = viewport.elementScrolled().subscribe(() => {
        this.syncHeaderScroll();
      });
      onCleanup(() => sub.unsubscribe());
    });

    // Re-arm the scroller whenever the data underneath it changes.
    //
    // One viewport instance serves every dataset the grid is handed —
    // switching worksheet tabs, or previewing a second file without
    // closing the pane. CDK picks up the new length (getDataLength()
    // reports it, and the spacer grows) but does not recompute the
    // rendered range, so the body stays pinned to the first screenful
    // while the scrollbar moves over the full height. Measured directly:
    // at scrollOffset 10000 of a 1,200-row sheet the rendered range was
    // still {start: 0, end: 32}, and checkViewportSize() corrected it to
    // {start: 309, end: 345} on the spot.
    //
    // Resetting to the top is the right behaviour on its own terms too —
    // a newly chosen sheet should start at its first row rather than
    // inheriting the previous one's offset.
    effect((onCleanup) => {
      const rows = this.rows();
      const viewport = this.viewport();
      if (!viewport || rows.length === 0) return;

      // After the rows themselves have been rendered: measuring before
      // that would measure the outgoing dataset.
      const handle = setTimeout(() => {
        viewport.scrollToOffset(0);
        viewport.checkViewportSize();
        const header = this.headerScroller()?.nativeElement;
        if (header) header.scrollLeft = 0;
      });
      onCleanup(() => clearTimeout(handle));
    });
  }

  /** `trackBy` on the row index rather than the row: a data file may
   *  legitimately repeat identical rows, and identity tracking would
   *  make the virtual scroller reuse the wrong one. */
  protected trackByIndex(index: number): number {
    return index;
  }

  /**
   * Mirror the viewport's horizontal offset onto the header.
   *
   * Driven by `elementScrolled()` rather than a template `(scroll)`
   * binding or `(scrolledIndexChange)`. `scrolledIndexChange` is the
   * wrong signal outright — it fires when the first *rendered row*
   * changes, so it never fires for a purely horizontal scroll and the
   * header stays behind while the body moves. `elementScrolled()` emits
   * for both axes, and CDK's scroll dispatcher already runs it outside
   * the Angular zone; since the handler only writes `scrollLeft` on a
   * DOM node and touches no signal, the sync costs no change detection
   * at scroll rate.
   */
  private syncHeaderScroll(): void {
    const header = this.headerScroller()?.nativeElement;
    const viewport = this.viewport();
    if (header && viewport) {
      header.scrollLeft = viewport.measureScrollOffset('left');
    }
  }
}

/** Enough room for the largest row number the gutter will show. */
function gutterWidthFor(rowCount: number): number {
  return Math.max(40, String(rowCount).length * 8 + 16);
}

/**
 * Width for one column, from the longest value in a sample of its cells.
 *
 * Approximated from character counts rather than measured, because
 * measuring means laying out every cell before the first paint. The
 * clamp matters more than the estimate: a column of long free text stops
 * at `COL_MAX_PX` and ellipsises (the full value is on the cell's
 * `title`), and a column of short codes still gets a readable minimum.
 */
function measureColumn(
  header: string,
  rows: readonly (readonly string[])[],
  index: number,
): number {
  let longest = header.length;
  const sampled = Math.min(rows.length, WIDTH_SAMPLE_ROWS);
  for (let i = 0; i < sampled; i++) {
    const cell = rows[i][index];
    if (cell && cell.length > longest) longest = cell.length;
  }
  return Math.min(
    COL_MAX_PX,
    Math.max(COL_MIN_PX, Math.round(longest * PX_PER_CHAR) + 24),
  );
}
