import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import {
  FilePreviewError,
  FilePreviewHttpService,
} from '../../../../services/file-preview/file-preview-http.service';
import { SheetPreview } from '../../../../services/file-preview/sheet-preview.model';
import { DataGridComponent } from './data-grid.component';

/**
 * Renders an `.xlsx` as a grid of values, read by app-api.
 *
 * The one viewer in the pane that does not parse anything. `.docx`,
 * `.pptx` and `.csv` all fetch bytes through a presigned URL and read
 * them in the browser; this one takes `uploadId` and asks
 * `/files/{id}/sheet-preview` for rows, because every client-side
 * workbook reader was rejected on its own terms — the npm build of
 * SheetJS is frozen on a 2022 release with unfixed advisories, and
 * ExcelJS raises outright on a workbook containing a native chart, which
 * is exactly what `create_excel_spreadsheet` produces. The distinction
 * that unblocked this was that a grid needs a *reader*, not a
 * *renderer*, and openpyxl is already what our own spreadsheet tools
 * drive.
 *
 * What the user does not get is fidelity: no fills, fonts, borders,
 * merges, column widths or charts. Download-and-open remains the path
 * for those, and the pane keeps its download button throughout.
 *
 * A formula whose value Excel never cached shows as its formula text
 * ("=SUM(B2:B10)") rather than as a blank. That is not a flourish — it
 * is the normal state of any workbook openpyxl wrote, since openpyxl
 * does not evaluate, so a values-only preview would show holes exactly
 * where the totals belong.
 */
@Component({
  selector: 'app-xlsx-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataGridComponent],
  template: `
    @if (active(); as sheet) {
      <div class="flex h-full flex-col">
        @if (sheets().length > 1) {
          <div
            class="flex shrink-0 gap-1 overflow-x-auto border-b border-gray-200 px-2 py-1.5 dark:border-gray-700"
            role="tablist"
            aria-label="Worksheets"
          >
            @for (candidate of sheets(); track candidate.name; let i = $index) {
              <button
                type="button"
                role="tab"
                class="shrink-0 rounded-2xl px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible"
                [class]="
                  i === selected()
                    ? 'bg-primary-accessible text-white'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                "
                [attr.aria-selected]="i === selected()"
                (click)="select(i)"
              >
                {{ candidate.name }}
              </button>
            }
          </div>
        }

        <div class="min-h-0 flex-1">
          <app-data-grid
            [headers]="sheet.headers"
            [rows]="sheet.rows"
            [summary]="summary()"
            [notice]="truncationNotice()"
            emptyMessage="This sheet is empty."
            [ariaLabel]="'Sheet ' + sheet.name + ', ' + sheet.rows.length + ' rows'"
          />
        </div>
      </div>
    }
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }
  `,
})
export class XlsxViewerComponent {
  /** The file to read. Unlike its sibling viewers this takes an id, not
   *  bytes — the workbook is never fetched into the browser. */
  readonly uploadId = input<string | null>(null);

  readonly rendered = output<void>();
  readonly renderFailed = output<string>();

  private readonly previewHttp = inject(FilePreviewHttpService);

  protected readonly sheets = signal<readonly SheetPreview[]>([]);
  protected readonly selected = signal(0);

  protected readonly active = computed<SheetPreview | null>(
    () => this.sheets()[this.selected()] ?? null,
  );

  protected readonly summary = computed(() => {
    const sheet = this.active();
    if (!sheet) return '';
    const shown = sheet.rows.length;
    const cols = sheet.headers.length;
    // `totalRows` is what the sheet claims; `rows.length` is what the
    // cap let through. Saying "500 of 12,480" is the honest form, and
    // collapsing to one number when they agree keeps the common case
    // quiet.
    const rowText =
      sheet.totalRows > shown
        ? `${shown.toLocaleString()} of ${sheet.totalRows.toLocaleString()} rows`
        : `${shown.toLocaleString()} ${shown === 1 ? 'row' : 'rows'}`;
    return `${rowText} · ${cols} ${cols === 1 ? 'column' : 'columns'} · values only`;
  });

  protected readonly truncationNotice = computed(() => {
    switch (this.active()?.truncatedBy) {
      case 'rows':
        return '· later rows not shown';
      case 'columns':
        return '· later columns not shown';
      default:
        return '';
    }
  });

  /** Bumped per load so a slow response for a file the pane has since
   *  moved off is discarded rather than painted. */
  private requestSeq = 0;

  constructor() {
    effect(() => {
      const uploadId = this.uploadId();
      this.requestSeq++;
      this.sheets.set([]);
      this.selected.set(0);
      if (uploadId) void this.load(uploadId, this.requestSeq);
    });
  }

  private async load(uploadId: string, seq: number): Promise<void> {
    try {
      const response = await this.previewHttp.fetchSheets(uploadId);
      if (seq !== this.requestSeq) return;

      if (response.sheets.length === 0) {
        // Every sheet hidden, or none readable. Blank grids would be a
        // worse answer than saying so.
        this.renderFailed.emit('This workbook has no visible sheets to show.');
        return;
      }

      this.sheets.set(response.sheets);
      this.rendered.emit();
    } catch (e) {
      if (seq !== this.requestSeq) return;
      this.renderFailed.emit(
        e instanceof FilePreviewError
          ? e.message
          : "This workbook couldn't be read.",
      );
    }
  }

  protected select(index: number): void {
    this.selected.set(index);
  }
}
