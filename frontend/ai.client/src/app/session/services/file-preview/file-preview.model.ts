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

/** What the pane knows how to render, and which viewer does it. */
export type PreviewKind = 'docx' | 'pptx';

/** Human label for the pane header's subtitle. */
export const PREVIEW_KIND_LABELS: Readonly<Record<PreviewKind, string>> = {
  docx: 'Word document',
  pptx: 'PowerPoint presentation',
};

/** The MIME type each viewer requires, checked against `/preview-url`. */
export const PREVIEW_KIND_MIMES: Readonly<Record<PreviewKind, string>> = {
  docx: DOCX_MIME,
  pptx: PPTX_MIME,
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
 * Legacy `.doc` and `.ppt` are deliberately excluded: they are the
 * pre-2007 binary formats, which the OOXML renderers cannot read at all.
 *
 * `.xlsx` is deliberately absent. There is no renderer for it we are
 * willing to ship: the npm build of SheetJS is frozen at a 2022 release
 * carrying unfixed advisories, and the only maintained grid renderer is
 * built on ExcelJS, which throws outright on the workbooks
 * `create_excel_spreadsheet` produces whenever one contains a native
 * chart. Download-and-open remains the path for spreadsheets.
 */
export function previewKindFor(filename: string): PreviewKind | null {
  const name = filename.trim();
  if (/\.docx$/i.test(name)) return 'docx';
  if (/\.pptx$/i.test(name)) return 'pptx';
  return null;
}

/** Whether a filename is one the preview pane can render. */
export function isPreviewableFilename(filename: string): boolean {
  return previewKindFor(filename) !== null;
}
