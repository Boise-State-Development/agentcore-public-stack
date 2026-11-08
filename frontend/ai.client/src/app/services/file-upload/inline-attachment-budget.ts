import {
  INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
  getFileExtension,
  isDocumentClassMimeType,
  isReferenceDocument,
  resolveMimeTypeFor,
} from './file-upload.service';

/** One attachment on the composer, as the inline budget judges it. */
export interface BudgetedAttachment {
  id: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

/** The attachments a message's combined inline budget leaves out. */
export interface InlineBudgetOverflow {
  /** Documents the assistant will read from storage instead of receiving in full. */
  byReference: BudgetedAttachment[];
  /** Files (images) that will not be included at all: they need a follow-up message. */
  dropped: BudgetedAttachment[];
}

const TABULAR_MIME_TYPES = new Set<string>([
  'text/csv',
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);
const TABULAR_EXTENSIONS = new Set<string>(['.csv', '.xls', '.xlsx']);
const PPTX_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';

/**
 * Whether the backend sends this file inline, so that it counts toward the
 * combined budget. Mirrors `_partition_attachments`: spreadsheets route to the
 * spreadsheet tools and decks to the PowerPoint tools, and a document over the
 * per-file inline limit is attached by reference; none of them is inline at any
 * size. Images are inline and count, as the server counts them.
 */
export function countsTowardInlineBudget(filename: string, mimeType: string, sizeBytes: number): boolean {
  const mime = resolveMimeTypeFor(filename, mimeType);
  const ext = getFileExtension(filename);
  if (TABULAR_MIME_TYPES.has(mime) || TABULAR_EXTENSIONS.has(ext)) return false;
  if (mime === PPTX_MIME_TYPE || ext === '.pptx') return false;
  if (isReferenceDocument(mime, sizeBytes)) return false;
  return true;
}

/**
 * Which of a message's attachments will not fit in its combined inline budget,
 * or `null` when they all do.
 *
 * The same first-fit walk the backend runs (`_apply_inline_byte_budget`): in
 * attachment order, keep each inline file that still fits and move the one that
 * would push the running total over. Earlier files win, and a later, smaller one
 * that still fits rides along. A document moved out is read from storage
 * instead (`INLINE_BUDGET_BY_REFERENCE_ENABLED` on the backend, default on); an
 * image has no such path and is left out. `attachments` must be in the order the
 * send carries them (`ChatInputComponent.attachmentIds`).
 */
export function inlineBudgetOverflow(
  attachments: readonly BudgetedAttachment[],
  maxTotalBytes: number = INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
): InlineBudgetOverflow | null {
  if (maxTotalBytes <= 0) return null;
  const byReference: BudgetedAttachment[] = [];
  const dropped: BudgetedAttachment[] = [];
  let running = 0;
  for (const attachment of attachments) {
    if (!countsTowardInlineBudget(attachment.filename, attachment.mimeType, attachment.sizeBytes)) continue;
    if (running + attachment.sizeBytes <= maxTotalBytes) {
      running += attachment.sizeBytes;
      continue;
    }
    const mime = resolveMimeTypeFor(attachment.filename, attachment.mimeType);
    (isDocumentClassMimeType(mime) ? byReference : dropped).push(attachment);
  }
  return byReference.length || dropped.length ? { byReference, dropped } : null;
}
