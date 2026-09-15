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
 * Whether a filename is one the preview pane can render.
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
 * Legacy `.doc` is deliberately excluded: it is the pre-2007 binary
 * format, which the OOXML renderer cannot read at all.
 */
export function isPreviewableFilename(filename: string): boolean {
  return /\.docx$/i.test(filename.trim());
}
