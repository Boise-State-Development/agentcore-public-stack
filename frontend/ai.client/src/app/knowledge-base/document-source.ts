import { Document } from '../assistants/models/document.model';

/**
 * Where a knowledge-base document came from: a device upload, a connected file
 * source (Google Drive), or a web page a crawl fetched. Read from the import
 * provenance on the record, so it needs nothing beyond what the list returns.
 */
export type DocumentSourceKind = 'upload' | 'connector' | 'web';

/** The sentinel connector id a crawled web page carries (`web_sources`). */
const WEB_CONNECTOR_ID = 'web';

export function documentSourceKind(doc: Document): DocumentSourceKind {
  if (doc.sourceConnectorId === WEB_CONNECTOR_ID) return 'web';
  if (doc.sourceConnectorId && doc.sourceFileId) return 'connector';
  return 'upload';
}

/**
 * What a row's primary "Open" does, most useful first:
 *
 * - **link**: the original, where it lives. A Drive file opens in Drive and a web
 *   page at its URL, which is what an author checking a source wants, rather than
 *   a copy of a copy.
 * - **view**: read a text document in the app. Markdown renders; plain text,
 *   CSV and SQL show as they are.
 * - **file**: anything else opens the stored file in a new tab, where the
 *   browser shows what it can (a PDF) and downloads the rest.
 *
 * Only a `complete` document has a stored file to view or open; a link out works
 * whatever the import's state.
 */
export type DocumentOpenAction =
  | { kind: 'link'; href: string; label: string }
  | { kind: 'view'; format: 'markdown' | 'text'; label: string }
  | { kind: 'file'; label: string }
  | { kind: 'none' };

/** Drive file ids are URL-safe base64ish; refuse anything else rather than build a URL from it. */
const DRIVE_ID = /^[A-Za-z0-9_-]+$/;

const MARKDOWN_EXTENSIONS = ['.md', '.markdown'];
const TEXT_EXTENSIONS = ['.txt', '.csv', '.sql'];

export function documentOpenAction(doc: Document, connectorName?: string): DocumentOpenAction {
  const kind = documentSourceKind(doc);
  if (kind === 'web') {
    const href = safeHttpUrl(doc.sourceFileId);
    if (href) return { kind: 'link', href, label: 'Open page' };
  }
  if (kind === 'connector' && doc.sourceAdapterKey === 'google-drive' && DRIVE_ID.test(doc.sourceFileId ?? '')) {
    return {
      kind: 'link',
      href: `https://drive.google.com/open?id=${encodeURIComponent(doc.sourceFileId!)}`,
      label: `Open in ${connectorName || 'Google Drive'}`,
    };
  }
  if (doc.status !== 'complete') return { kind: 'none' };
  const name = doc.filename.toLowerCase();
  if (MARKDOWN_EXTENSIONS.some((ext) => name.endsWith(ext)) || doc.contentType === 'text/markdown') {
    return { kind: 'view', format: 'markdown', label: 'View' };
  }
  if (TEXT_EXTENSIONS.some((ext) => name.endsWith(ext)) || doc.contentType === 'text/plain') {
    return { kind: 'view', format: 'text', label: 'View' };
  }
  return { kind: 'file', label: 'Open file' };
}

/** `value` as an absolute http(s) URL, or null. Never builds a `javascript:` href. */
function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}
