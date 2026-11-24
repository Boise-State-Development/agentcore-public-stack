import type { SourceLocator } from '../../../shared/utils/stream-parser';
import { previewKindFor, type OpenFilePreviewRef } from './file-preview.model';

/** What a cited value carries that can name a page: a `verify_sources`
 *  claim or a `document_extract` field, with the document it came from. */
export interface PageCitation {
  state: 'supported' | 'quote_unsupported' | 'unassessed' | 'unverified';
  locator: SourceLocator | null;
  uploadId: string;
  filename: string;
  quote?: string | null;
}

/**
 * The preview request that opens a cited PDF at the cited page, with its
 * quote to highlight, or null when the citation cannot be opened that way.
 *
 * Shared by `SourceProvenanceComponent` and `DocumentExtractionComponent`
 * so the two never disagree about which locators are links. Only a located
 * passage (never `unverified`) in a PDF the pane can show, with a page
 * locator: a `.docx` locator is a paragraph no viewer can scroll to, and a
 * link that opened such a document at the top would claim a precision it
 * does not have.
 */
export function pdfPageRef(citation: PageCitation): OpenFilePreviewRef | null {
  const { state, locator, uploadId, filename } = citation;
  if (state === 'unverified' || !locator || locator.kind !== 'page') return null;
  if (!uploadId || previewKindFor(filename ?? '') !== 'pdf') return null;
  if (!Number.isInteger(locator.value) || locator.value < 1) return null;
  const quote = citation.quote?.trim();
  return { uploadId, filename, page: locator.value, ...(quote ? { quote } : {}) };
}
