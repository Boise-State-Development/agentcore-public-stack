/**
 * The shape `GET /files/{uploadId}/sheet-preview` returns.
 *
 * Mirrors `SheetPreview` / `SheetPreviewResponse` in
 * `apis/shared/files/models.py`, which serialise with camelCase aliases.
 * Every cell is a string the server already formatted for display —
 * dates, floats and booleans included — so the grid never has to decide
 * how a value should read, and the two readers behind it (openpyxl here,
 * the browser's own parser for `.csv`) cannot drift apart on it.
 */
export interface SheetPreview {
  /** Worksheet name, as it appears on the tab in Excel. */
  name: string;
  /** First row of the sheet, used as column labels. */
  headers: string[];
  /** Body rows, each padded to `headers.length`. */
  rows: string[][];
  /** Body rows the sheet claims to have, which may exceed `rows.length`. */
  totalRows: number;
  /** True when a cap stopped the read short of the sheet's end. */
  truncated: boolean;
  /** Which cap fired. */
  truncatedBy: 'rows' | 'columns' | null;
}

export interface SheetPreviewResponse {
  uploadId: string;
  filename: string;
  /** Visible worksheets, in workbook order. Hidden sheets are omitted. */
  sheets: SheetPreview[];
  truncated: boolean;
}
