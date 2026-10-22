import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  output,
  signal,
} from '@angular/core';
import {
  CsvParseError,
  CsvTable,
  parseCsv,
} from '../../../../services/file-preview/csv-parse';
import { DataGridComponent } from './data-grid.component';

/**
 * Renders a delimited data file (`.csv`) as a scrollable grid.
 *
 * Parsing happens in the browser: a delimited file needs nothing but
 * field splitting, and `csv-parse.ts` is a couple of hundred lines with
 * no dependency. That is the line between this viewer and the `.xlsx`
 * one, which has to ask the server — not because spreadsheets are
 * bigger, but because no client-side workbook *reader* was shippable.
 *
 * Everything below the data is `DataGridComponent`'s job.
 */
@Component({
  selector: 'app-csv-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataGridComponent],
  template: `
    @if (table(); as data) {
      <app-data-grid
        [headers]="data.headers"
        [rows]="data.rows"
        [summary]="summary()"
        [notice]="truncationNotice()"
        emptyMessage="This file has column headers but no data rows."
        [ariaLabel]="'Data preview, ' + data.rows.length + ' rows'"
      />
    }
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }
  `,
})
export class CsvViewerComponent {
  readonly bytes = input<ArrayBuffer | null>(null);

  /** Emitted once a grid is on screen, so the pane drops its skeleton. */
  readonly rendered = output<void>();
  /** Emitted with a user-facing message when the file cannot be read. */
  readonly renderFailed = output<string>();

  protected readonly table = signal<CsvTable | null>(null);

  protected readonly summary = computed(() => {
    const data = this.table();
    if (!data) return '';
    const rows = data.rows.length;
    const cols = data.headers.length;
    return `${rows.toLocaleString()} ${rows === 1 ? 'row' : 'rows'} · ${cols} ${
      cols === 1 ? 'column' : 'columns'
    } · ${DELIMITER_LABELS[data.delimiter]}`;
  });

  protected readonly truncationNotice = computed(() => {
    const by = this.table()?.truncatedBy;
    switch (by) {
      case 'bytes':
        return '· preview stops partway through a large file';
      case 'rows':
        return '· later rows not shown';
      case 'columns':
        return '· later columns not shown';
      default:
        return '';
    }
  });

  constructor() {
    effect(() => {
      const bytes = this.bytes();
      if (!bytes) {
        this.table.set(null);
        return;
      }

      try {
        this.table.set(parseCsv(bytes));
      } catch (e) {
        this.table.set(null);
        this.renderFailed.emit(
          e instanceof CsvParseError
            ? e.message
            : "This file couldn't be read as delimited text.",
        );
        return;
      }

      this.rendered.emit();
    });
  }
}

const DELIMITER_LABELS: Readonly<Record<string, string>> = {
  ',': 'comma-separated',
  '\t': 'tab-separated',
  ';': 'semicolon-separated',
  '|': 'pipe-separated',
};
