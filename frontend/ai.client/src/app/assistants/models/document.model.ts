/**
 * Document status types matching backend DocumentStatus
 */
export type DocumentStatus = 'uploading' | 'chunking' | 'embedding' | 'complete' | 'failed';

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
}

/**
 * Response from GET /assistants/{assistantId}/documents
 */
export interface DocumentsListResponse {
  documents: Document[];
  nextToken?: string;
}

