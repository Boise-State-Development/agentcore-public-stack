/**
 * Document status types matching backend DocumentStatus
 *
 * `provisioning` is the leading status of a born-managed first upload
 * (MANAGED_KB_NEW_DEFAULT): the file is uploaded and the assistant's knowledge
 * base is still being created. Only ever the FIRST document of an assistant —
 * every upload after that starts at `uploading`.
 */
export type DocumentStatus =
  | 'provisioning'
  | 'uploading'
  | 'chunking'
  | 'embedding'
  | 'complete'
  | 'failed';

/**
 * Statuses that indicate a document is still being processed.
 */
export const PROCESSING_STATUSES: readonly DocumentStatus[] = [
  'provisioning',
  'uploading',
  'chunking',
  'embedding',
] as const;

/**
 * Threshold (ms) after which a processing document is considered stale.
 * Must exceed the Lambda ingestion timeout (900s / 15 min) to avoid
 * killing in-flight jobs. Matches backend STALE_PROCESSING_TIMEOUT_MINUTES (20 min).
 */
export const STALE_DOCUMENT_THRESHOLD_MS = 20 * 60 * 1000;

/**
 * Request body for POST /assistants/{assistantId}/documents/upload-url
 */
export interface CreateDocumentRequest {
  filename: string;
  contentType: string;
  sizeBytes: number;
}

/**
 * Response from POST /assistants/{assistantId}/documents/upload-url
 */
export interface UploadUrlResponse {
  documentId: string;
  uploadUrl: string;
  expiresIn: number;
}

/**
 * Document response model matching backend DocumentResponse
 */
export interface Document {
  documentId: string;
  assistantId: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  status: DocumentStatus;
  errorMessage?: string;
  errorDetails?: string;
  chunkCount?: number;
  createdAt: string;
  updatedAt: string;
  /**
   * Import provenance — set only for documents imported from an external
   * source (Google Drive, web crawl); all null/absent for device uploads.
   * A document is a syncable Drive source when `sourceFileId` is present
   * and `sourceConnectorId` is a real connector (web pages use the
   * sentinel connector id 'web' and sync at the crawl level instead).
   */
  sourceConnectorId?: string | null;
  sourceAdapterKey?: string | null;
  sourceFileId?: string | null;
  /** Back-pointer to the covering sync policy, when one exists. */
  syncPolicyId?: string | null;
  lastSyncedAt?: string | null;
}

/**
 * Response from GET /assistants/{assistantId}/documents
 */
export interface DocumentsListResponse {
  documents: Document[];
  nextToken?: string;
}

/**
 * Response from GET /assistants/{assistantId}/documents/{documentId}/download
 */
export interface DownloadUrlResponse {
  downloadUrl: string;
  filename: string;
  expiresIn: number;
}

/**
 * One passage as the knowledge base actually holds it.
 *
 * `text` is the FULL extracted text. The per-answer citation trace caps excerpts at
 * 500 characters, which is precisely why it cannot serve this purpose: a flattened
 * table's damage is usually past the cut, so a truncated excerpt of a mangled table
 * reads like a fine excerpt of a fine table.
 */
export interface ExtractedChunk {
  text: string;
  /** Position in the returned set — NOT the document's own order. */
  order: number;
  score?: number | null;
  /** Page when the backend supplied one. Never inferred. */
  page?: number | null;
}

/**
 * Response from GET /assistants/{assistantId}/documents/{documentId}/chunks
 *
 * `available` is false with a `reason` when the engine cannot show a single
 * document's chunks — a classic knowledge base cannot scope a retrieval to one
 * document, and approximating would render other documents' content under this
 * document's name. The shape is identical either way so the UI never branches on
 * engine.
 *
 * `capReached` is honesty rather than a paging hint: Bedrock exposes no
 * chunk-enumeration API, so a complete set is never guaranteed.
 */
export interface ExtractedChunksResponse {
  documentId: string;
  fileName: string;
  engine: string;
  available: boolean;
  reason?: string | null;
  chunks: ExtractedChunk[];
  returned: number;
  capReached: boolean;
}

