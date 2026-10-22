/** The file the docked preview pane is showing. */
export interface OpenFilePreviewRef {
  /** User-files upload id — the owner-scoped handle every route keys on. */
  uploadId: string;
  /** Display name, used for the pane header and the download filename. */
  filename: string;
}

/**
 * MIME type of a Word document (OOXML). Matches
 * `apis.shared.files.ALLOWED_MIME_TYPES` and the `_DOCX_MIME` constant in
 * `agents/builtin_tools/word_document_tool.py`.
 */
export const DOCX_MIME =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

/**
 * MIME type of a PowerPoint presentation (OOXML). Matches
 * `apis.shared.files.ALLOWED_MIME_TYPES` and the `_PPTX_MIME` constant in
 * `agents/builtin_tools/powerpoint_presentation_tool.py`.
 */
export const PPTX_MIME =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation';

/**
 * MIME type of a comma-separated values file. Matches the `.csv` entry
 * in `apis.shared.files.ALLOWED_EXTENSIONS`.
 */
export const CSV_MIME = 'text/csv';

/**
 * MIME type of an Excel workbook (OOXML). Matches the `.xlsx` entry in
 * `apis.shared.files.ALLOWED_EXTENSIONS` and `SHEET_PREVIEW_MIME_TYPES`
 * in the same module.
 */
export const XLSX_MIME =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

/** What the pane knows how to render, and which viewer does it. */
export type PreviewKind = 'docx' | 'pptx' | 'csv' | 'xlsx';

/** Human label for the pane header's subtitle. */
export const PREVIEW_KIND_LABELS: Readonly<Record<PreviewKind, string>> = {
  docx: 'Word document',
  pptx: 'PowerPoint presentation',
  csv: 'Data file',
  xlsx: 'Spreadsheet',
};

/**
 * The MIME types each viewer will accept, checked against what
 * `/preview-url` reports.
 *
 * A list rather than a single type because the recorded MIME is
 * whatever the *browser* reported at upload time (`request.mime_type`
 * in `files/service.py`), not something the server derives from the
 * bytes. For the OOXML formats that is reliably the one true type. For
 * `.csv` it is not: Windows reports `application/vnd.ms-excel` for a
 * `.csv` whenever Excel is the registered handler, and some clients
 * send `application/csv` or fall back to `text/plain`. Rejecting those
 * would fail the preview on the most ordinary desktop in the building,
 * for a file whose extension already told us what it is.
 */
export const PREVIEW_KIND_MIMES: Readonly<
  Record<PreviewKind, readonly string[]>
> = {
  docx: [DOCX_MIME],
  pptx: [PPTX_MIME],
  csv: [CSV_MIME, 'application/csv', 'application/vnd.ms-excel', 'text/plain'],
  xlsx: [XLSX_MIME],
};

/**
 * Which viewer a filename maps to, or null if the pane can't render it.
 *
 * Extension-based rather than MIME-based on purpose: the inline download
 * card is rendered from a persisted tool payload that carries only
 * `filename` and `upload_id` — the MIME type isn't in it, and asking the
 * server for one just to decide whether to show a button would put a
 * request behind every card. The authoritative MIME check still happens
 * in `FilePreviewHttpService.fetchDocument`, against what
 * `/preview-url` reports, so a mislabelled `.docx` fails there rather
 * than feeding garbage to the renderer.
 *
 * Legacy `.doc`, `.ppt` and `.xls` are deliberately excluded: they are
 * the pre-2007 binary formats, which neither the OOXML renderers nor
 * the delimited-text parser can read at all.
 *
 * `.xlsx` is previewed, but not like the others: it is the one kind
 * whose bytes never reach the browser. No client-side workbook renderer
 * was shippable — the npm build of SheetJS is frozen at a 2022 release
 * carrying unfixed advisories, and the only maintained grid renderer is
 * built on ExcelJS, which throws outright on the workbooks
 * `create_excel_spreadsheet` produces whenever one contains a native
 * chart — so app-api reads it with openpyxl and sends rows instead. Any
 * caller that branches on MIME type for `.xlsx` is therefore on the
 * wrong path; branch on the kind, and see `XlsxViewerComponent`.
 */
export function previewKindFor(filename: string): PreviewKind | null {
  const name = filename.trim();
  if (/\.docx$/i.test(name)) return 'docx';
  if (/\.pptx$/i.test(name)) return 'pptx';
  if (/\.csv$/i.test(name)) return 'csv';
  if (/\.xlsx$/i.test(name)) return 'xlsx';
  return null;
}

/**
 * Whether the pane reads this kind from bytes it fetched itself.
 *
 * False only for `xlsx`, whose viewer takes an upload id and asks the
 * server for rows. The panel uses this to decide whether to run the
 * presigned-URL fetch at all — without it, opening a workbook would
 * download the whole file to the browser and then ignore it.
 */
export function previewFetchesBytes(kind: PreviewKind): boolean {
  return kind !== 'xlsx';
}

/** Whether a filename is one the preview pane can render. */
export function isPreviewableFilename(filename: string): boolean {
  return previewKindFor(filename) !== null;
}
