/**
 * Presentation for one artifact content type.
 *
 * Colors are `filetype-*` identity tokens, not brand tokens: a Markdown
 * badge means "this is Markdown" and must not follow a rebrand, the same
 * reason `file-card.component.ts` uses them for attachments. Reusing the
 * same hues keeps one artifact and one uploaded file of the same type
 * reading as the same kind of thing.
 */
export interface TypeStyle {
  readonly label: string;
  readonly icon: string;
  readonly bg: string;
  readonly text: string;
}

const TYPE_STYLES: Record<string, TypeStyle> = {
  'text/markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-700 dark:text-filetype-markdown-300',
  },
  'text/x-markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-700 dark:text-filetype-markdown-300',
  },
  'text/html': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-700 dark:text-filetype-code-300',
  },
  'application/xhtml+xml': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-700 dark:text-filetype-code-300',
  },
  'text/csv': {
    label: 'CSV',
    icon: 'heroTableCells',
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-700 dark:text-filetype-sheet-300',
  },
  'image/svg+xml': {
    label: 'SVG',
    icon: 'heroPhoto',
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-700 dark:text-filetype-image-300',
  },
};

const DEFAULT_TYPE_STYLE: TypeStyle = {
  label: 'Text',
  icon: 'heroDocument',
  bg: 'bg-gray-100 dark:bg-gray-700',
  text: 'text-gray-600 dark:text-gray-300',
};

/** Strips the `; charset=…` the writer stores on HTML content types. */
export function normalizeArtifactContentType(contentType: string): string {
  return contentType.split(';')[0].trim().toLowerCase();
}

/** How an artifact of this content type is labelled, iconed and coloured. */
export function artifactTypeStyle(contentType: string): TypeStyle {
  return TYPE_STYLES[normalizeArtifactContentType(contentType)] ?? DEFAULT_TYPE_STYLE;
}
