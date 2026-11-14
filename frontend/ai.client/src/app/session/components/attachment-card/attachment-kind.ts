import {
  DEFAULT_STYLE,
  FILE_TYPE_STYLES,
} from '../message-list/components/file-attachment/file-attachment-badge.component';

/** What the card's preview draws for a file. */
export type AttachmentPreviewKind =
  | 'image'
  | 'pdf'
  | 'markdown'
  | 'text'
  | 'csv'
  | 'deck'
  | 'sheet'
  | 'doc'
  | 'generic';

export interface AttachmentKind {
  preview: AttachmentPreviewKind;
  /** Short type label for the meta line: PDF, DOCX, CSV. */
  label: string;
  /** What the chrome row's locator says, in words. */
  name: string;
  icon: string;
  /** The type's accent, applied to a wrapper so `ng-icon` inherits it. */
  accent: string;
}

const PREVIEW_BY_MIME: Record<string, { preview: AttachmentPreviewKind; name: string }> = {
  'application/pdf': { preview: 'pdf', name: 'PDF document' },
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {
    preview: 'doc',
    name: 'Word document',
  },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': {
    preview: 'sheet',
    name: 'Spreadsheet',
  },
  'application/vnd.ms-excel': { preview: 'sheet', name: 'Spreadsheet' },
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': {
    preview: 'deck',
    name: 'Slide deck',
  },
  'text/csv': { preview: 'csv', name: 'CSV table' },
  'text/markdown': { preview: 'markdown', name: 'Markdown' },
  'text/html': { preview: 'text', name: 'Web page' },
  'text/plain': { preview: 'text', name: 'Plain text' },
};

/** The card's view of a MIME type: how to preview it and what to call it. */
export function attachmentKind(mimeType: string): AttachmentKind {
  const style = FILE_TYPE_STYLES[mimeType] ?? DEFAULT_STYLE;
  const isImage = mimeType.startsWith('image/');
  const known = PREVIEW_BY_MIME[mimeType];
  return {
    preview: isImage ? 'image' : (known?.preview ?? 'generic'),
    label: style.label,
    name: isImage ? 'Image' : (known?.name ?? 'File'),
    icon: style.icon,
    accent: style.accent_text,
  };
}

/** Preview kinds whose opening text can be read from the file itself. */
export const TEXT_PREVIEW_KINDS: ReadonlySet<AttachmentPreviewKind> = new Set([
  'markdown',
  'text',
  'csv',
]);

/** Bytes of a local file read for its text preview. */
export const LOCAL_SNIPPET_BYTES = 4096;

/**
 * The first rows of a CSV, split on commas, for the card's little table. A
 * preview, not a parser: a quoted comma splits a cell, which only misplaces
 * text in a thumbnail.
 */
export function csvPreviewRows(text: string, rows = 6, cols = 4): string[][] {
  return text
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .slice(0, rows)
    .map((line) =>
      line
        .split(',')
        .slice(0, cols)
        .map((cell) => cell.trim().replace(/^"|"$/g, '')),
    );
}
