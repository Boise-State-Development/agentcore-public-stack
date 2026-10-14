import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { FEATURES, type FeatureFlags } from '../features';
/**
 * File status enum matching backend FileStatus
 */
export type FileStatus = 'pending' | 'ready' | 'failed';

/**
 * Allowed MIME types for file uploads.
 *
 * Must stay in sync with `ALLOWED_MIME_TYPES` in
 * `backend/src/apis/shared/files/models.py` — the backend is the enforcing
 * side, this list drives the picker's `accept` filter and the drop/paste
 * check. Not every entry is sent to Bedrock as a document block: csv/xls/xlsx
 * route to the spreadsheet tools and pptx routes to the PowerPoint tools
 * (Bedrock's document-format enum has no `pptx`). See `_partition_attachments`.
 */
export const ALLOWED_MIME_TYPES: Record<string, string> = {
  // Documents
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'text/plain': 'txt',
  'text/html': 'html',
  'text/csv': 'csv',
  'application/vnd.ms-excel': 'xls',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  'text/markdown': 'md',
  // Images (Bedrock-supported)
  'image/png': 'png',
  'image/jpeg': 'jpeg',
  'image/gif': 'gif',
  'image/webp': 'webp',
};

/**
 * Allowed file extensions
 */
export const ALLOWED_EXTENSIONS = [
  // Documents
  '.pdf', '.docx', '.txt', '.html', '.csv', '.xls', '.xlsx', '.pptx', '.md',
  // Images
  '.png', '.jpg', '.jpeg', '.gif', '.webp'
];

/**
 * Maximum file size in bytes (4MB)
 */
export const MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024;

/**
 * Maximum .pptx size in bytes (25MB).
 *
 * Decks get a larger cap because the general limit exists to keep documents
 * under what Bedrock accepts as an inline content block, and a .pptx is
 * never sent inline — it routes to the PowerPoint tools instead. Corporate
 * templates with imagery routinely clear 4MB, which is what made the
 * `template_name` workflow unusable.
 *
 * Must match `FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION` on the backend
 * (`files/service.py`), which is the enforcing side — if this is the larger
 * of the two, the UI accepts a deck that presign then rejects.
 */
export const PPTX_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024;

/**
 * Maximum size of a large document attached by reference (50MB).
 *
 * A document over `MAX_FILE_SIZE_BYTES` never rides inline: the backend
 * prepares it off the request path and the model reads the parts it needs
 * through `document_read` (docs/specs/large-document-attachments.md). Admitted
 * only while `features.largeDocumentAttachments` is on.
 *
 * Must match `LARGE_DOCUMENT_MAX_BYTES` on the backend
 * (`backend/src/apis/shared/files/models.py`), which is the enforcing side.
 */
export const LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024;

/**
 * MIME types `document_read` can read: the backend's `DOCUMENT_CLASS_FORMATS`
 * (pdf, docx, txt, md, html), as `document_format_for` classifies them. CSV is
 * text but tabular, so it routes to the spreadsheet tools instead.
 */
const DOCUMENT_CLASS_MIME_TYPES = new Set<string>([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/plain',
  'text/markdown',
  'text/html',
]);

/** Whether a file of this MIME type is a document `document_read` can read. */
export function isDocumentClassMimeType(mimeType: string): boolean {
  return DOCUMENT_CLASS_MIME_TYPES.has(mimeType);
}

/**
 * Whether a file is attached by reference: a document over the inline limit
 * and within the large-document ceiling. Mirrors the backend's one predicate,
 * `is_reference_document`, for display only (the attachment chip); the backend
 * decides how the file actually reaches the model.
 */
export function isReferenceDocument(mimeType: string, sizeBytes: number): boolean {
  return (
    isDocumentClassMimeType(mimeType) &&
    sizeBytes > MAX_FILE_SIZE_BYTES &&
    sizeBytes <= LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES
  );
}

/**
 * Resolve the size cap that applies to a given file. Use this rather than
 * `MAX_FILE_SIZE_BYTES` directly at any gate, or a legitimate deck (or, with
 * large-document attachments on, a large PDF) gets rejected by whichever check
 * was missed. `features` comes from the `FEATURES` token at the call site.
 */
export function maxFileSizeFor(
  file: File,
  features: Pick<FeatureFlags, 'largeDocumentAttachments'>,
): number {
  return attachmentSizeLimitFor(file.name, file.type, features);
}

/**
 * The same cap, for a file known only by name and MIME type: an entry in a
 * connected file source's listing, before it is downloaded.
 */
export function attachmentSizeLimitFor(
  filename: string,
  mimeType: string,
  features: Pick<FeatureFlags, 'largeDocumentAttachments'>,
): number {
  const isPptx =
    filename.toLowerCase().endsWith('.pptx') ||
    mimeType === 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
  if (isPptx) {
    return PPTX_MAX_FILE_SIZE_BYTES;
  }
  if (
    features.largeDocumentAttachments &&
    isDocumentClassMimeType(resolveMimeTypeFor(filename, mimeType))
  ) {
    return LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES;
  }
  return MAX_FILE_SIZE_BYTES;
}

/** The shape of a file-source listing entry the composer needs to judge. */
export interface AttachableSourceFile {
  name: string;
  mimeType?: string | null;
  /** What the file downloads as when an export changes it (a Google Doc → .docx). */
  downloadMimeType?: string | null;
  /** Unknown (null) for a provider-native document until it is exported. */
  sizeBytes?: number | null;
}

/**
 * Whether a file listed by a connected file source can be attached to a
 * conversation: its downloaded type is one the chat accepts, and its size,
 * when the provider knows it, is within that type's cap. Mirrors the checks
 * `processFiles` runs on a device upload, so the browser greys a file out
 * instead of letting the import fail after the download. The backend
 * re-checks both once the bytes are in hand.
 */
export function isAttachableSourceFile(
  file: AttachableSourceFile,
  features: Pick<FeatureFlags, 'largeDocumentAttachments'>,
): boolean {
  const mime = resolveMimeTypeFor(file.name, file.downloadMimeType || file.mimeType || '');
  if (!isAllowedMimeType(mime)) {
    return false;
  }
  if (file.sizeBytes != null && file.sizeBytes > attachmentSizeLimitFor(file.name, mime, features)) {
    return false;
  }
  return true;
}

/**
 * Maximum files per message
 */
export const MAX_FILES_PER_MESSAGE = 5;

/**
 * Request body for POST /files/presign
 */
export interface PresignRequest {
  sessionId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

/**
 * Response from POST /files/presign
 */
export interface PresignResponse {
  uploadId: string;
  presignedUrl: string;
  expiresAt: string;
}

/**
 * Response from POST /files/{uploadId}/complete
 */
export interface CompleteUploadResponse {
  uploadId: string;
  status: string;
  s3Uri: string;
  filename: string;
  sizeBytes: number;
}

/**
 * Response from GET /files/{uploadId}/preview-url
 */
export interface PreviewUrlResponse {
  uploadId: string;
  url: string;
  expiresAt: string;
  mimeType: string;
  filename: string;
}

/**
 * Response from GET /files/{uploadId}/text-snippet
 */
export interface TextSnippetResponse {
  uploadId: string;
  snippet: string;
  truncated: boolean;
  mimeType: string;
}

/**
 * Response from GET /files/{uploadId}/thumbnail
 */
export interface ThumbnailResponse {
  uploadId: string;
  url: string;
  expiresAt: string;
  cached: boolean;
}

/**
 * Outcome of a thumbnail fetch — `unsupported` (415) and `unavailable`
 * (404/422/network) collapse into typed states the UI can switch on
 * without parsing HTTP errors at the call site.
 */
export type ThumbnailFetchResult =
  | { status: 'ready'; response: ThumbnailResponse }
  | { status: 'unsupported' }
  | { status: 'unavailable' };

/**
 * File metadata from list/get operations
 */
export interface FileMetadata {
  uploadId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  sessionId: string;
  s3Uri: string;
  status: FileStatus;
  createdAt: string;
}

/**
 * Response from GET /files
 */
export interface FileListResponse {
  files: FileMetadata[];
  nextCursor: string | null;
  totalCount: number | null;
}

/**
 * Response from GET /files/quota
 */
export interface QuotaResponse {
  usedBytes: number;
  maxBytes: number;
  fileCount: number;
}

/**
 * Pending upload state for UI tracking
 */
export interface PendingUpload {
  file: File;
  uploadId: string;
  status: 'uploading' | 'completing' | 'ready' | 'error';
  progress: number;
  error?: string;
  /**
   * What the card says while `uploading`, instead of "Uploading... n%". Set
   * when the bytes never leave this browser — a file app-api fetches from a
   * connected file source — so there is no upload and no percentage to show.
   */
  statusLabel?: string;
}

/**
 * Error types for file uploads
 */
export class FileUploadError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(message);
    this.name = 'FileUploadError';
  }
}

export class FileTooLargeError extends FileUploadError {
  constructor(sizeBytes: number, maxSize: number) {
    super(
      `File too large: ${formatBytes(sizeBytes)}. Maximum: ${formatBytes(maxSize)}`,
      'FILE_TOO_LARGE',
      { sizeBytes, maxSize }
    );
  }
}

export class InvalidFileTypeError extends FileUploadError {
  constructor(mimeType: string) {
    super(
      `Invalid file type: ${mimeType}. Allowed: ${ALLOWED_EXTENSIONS.join(', ')}`,
      'INVALID_FILE_TYPE',
      { mimeType, allowed: ALLOWED_EXTENSIONS }
    );
  }
}

export class QuotaExceededError extends FileUploadError {
  constructor(currentUsage: number, maxAllowed: number, requiredSpace: number) {
    super(
      `Storage quota exceeded: ${formatBytes(currentUsage)}/${formatBytes(maxAllowed)}`,
      'QUOTA_EXCEEDED',
      { currentUsage, maxAllowed, requiredSpace }
    );
  }
}

/**
 * Format bytes to human-readable string
 */
export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

/**
 * Check if MIME type is allowed
 */
export function isAllowedMimeType(mimeType: string): boolean {
  return mimeType in ALLOWED_MIME_TYPES;
}

/**
 * Get file extension from filename
 */
export function getFileExtension(filename: string): string {
  const lastDot = filename.lastIndexOf('.');
  return lastDot >= 0 ? filename.slice(lastDot).toLowerCase() : '';
}

/**
 * Extension → MIME map, mirroring `ALLOWED_EXTENSIONS` in
 * `backend/src/apis/shared/files/models.py`.
 */
const EXTENSION_TO_MIME_TYPE: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.txt': 'text/plain',
  '.html': 'text/html',
  '.csv': 'text/csv',
  '.xls': 'application/vnd.ms-excel',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.md': 'text/markdown',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
};

/**
 * Resolve the MIME type to send with a presign request.
 *
 * Browsers do not always populate `File.type` — an OS with no handler
 * registered for the extension reports `''`, and some file managers report
 * `application/octet-stream`. `validateFile` accepts those by extension, so
 * sending the browser's value verbatim would hand the backend a MIME its
 * own allowlist rejects, 400-ing an upload the UI just accepted.
 *
 * Resolving from the extension also keeps files reachable by the tools that
 * route them, which match on the stored MIME *exactly*: a deck stored as
 * octet-stream is invisible to `read_powerpoint_presentation`, and a
 * spreadsheet stored that way never reaches the analysis tools.
 */
export function resolveMimeType(file: File): string {
  return resolveMimeTypeFor(file.name, file.type);
}

/** `resolveMimeType` for a file known by name and reported type only. */
export function resolveMimeTypeFor(filename: string, mimeType: string): string {
  if (isAllowedMimeType(mimeType)) {
    return mimeType;
  }
  return EXTENSION_TO_MIME_TYPE[getFileExtension(filename)] ?? mimeType;
}

/**
 * Service for managing file uploads via pre-signed URLs.
 *
 * Upload flow:
 * 1. Client calls requestPresignedUrl() with file metadata
 * 2. Backend validates, creates pending record, returns pre-signed URL
 * 3. Client uploads directly to S3 using pre-signed URL
 * 4. Client calls completeUpload() to mark as ready
 */
@Injectable({
  providedIn: 'root'
})
export class FileUploadService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private features = inject(FEATURES);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/files`);

  // State signals
  private _pendingUploads = signal<Map<string, PendingUpload>>(new Map());
  private _quota = signal<QuotaResponse | null>(null);
  private _loading = signal(false);
  private _error = signal<string | null>(null);

  // Public readonly signals
  readonly pendingUploads = this._pendingUploads.asReadonly();
  readonly quota = this._quota.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  // Computed signals
  readonly pendingUploadsList = computed(() =>
    Array.from(this._pendingUploads().values())
  );

  readonly hasActivePendingUploads = computed(() =>
    this.pendingUploadsList().some(u => u.status === 'uploading' || u.status === 'completing')
  );

  readonly readyUploads = computed(() =>
    this.pendingUploadsList().filter(u => u.status === 'ready')
  );

  readonly readyUploadIds = computed(() =>
    this.readyUploads().map(u => u.uploadId)
  );

  readonly quotaUsagePercent = computed(() => {
    const q = this._quota();
    if (!q || q.maxBytes === 0) return 0;
    return Math.min(100, (q.usedBytes / q.maxBytes) * 100);
  });

  /**
   * Get a ready file by its upload ID.
   * Returns null if the file is not found or not ready.
   *
   * @param uploadId - The upload ID to look up
   * @returns FileMetadata if found and ready, null otherwise
   */
  getReadyFileById(uploadId: string): FileMetadata | null {
    const pending = this._pendingUploads().get(uploadId);
    if (pending && pending.status === 'ready') {
      return {
        uploadId: pending.uploadId,
        sessionId: '', // Session ID not tracked in PendingUpload
        filename: pending.file.name,
        mimeType: pending.file.type || 'application/octet-stream',
        sizeBytes: pending.file.size,
        s3Uri: '', // S3 URI not available in client-side pending upload
        status: 'ready',
        createdAt: new Date().toISOString(),
      };
    }
    return this.knownFiles.get(uploadId) ?? null;
  }

  /**
   * Files the composer holds as server metadata rather than an upload from
   * this browser: one restored from a draft, or one app-api fetched from a
   * connected file source. The thread's user message draws its attachment
   * cards from `getReadyFileById`, so without this a file that never went
   * through `uploadFile` sent fine but showed no card.
   */
  private readonly knownFiles = new Map<string, FileMetadata>();

  /** Make `getReadyFileById` answer for a file whose bytes never left this browser. */
  registerKnownFile(file: FileMetadata): void {
    this.knownFiles.set(file.uploadId, file);
  }

  /**
   * Validate a file before upload.
   * @throws FileUploadError if validation fails
   */
  validateFile(file: File): void {
    // Check size (pptx and, behind the flag, large documents have their own caps)
    const sizeLimit = maxFileSizeFor(file, this.features);
    if (file.size > sizeLimit) {
      throw new FileTooLargeError(file.size, sizeLimit);
    }

    // Check MIME type
    if (!isAllowedMimeType(file.type)) {
      // Also check by extension if MIME type is generic
      const ext = getFileExtension(file.name);
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        throw new InvalidFileTypeError(file.type || ext || 'unknown');
      }
    }
  }

  /**
   * Upload a file for a session.
   *
   * @param sessionId - The session to associate the file with
   * @param file - The file to upload
   * @returns The upload metadata after completion
   * @throws FileUploadError on validation or upload failure
   */
  async uploadFile(sessionId: string, file: File): Promise<CompleteUploadResponse> {
    // Validate
    this.validateFile(file);

    this._error.set(null);

    // Step 1: Request pre-signed URL
    const presignRequest: PresignRequest = {
      sessionId,
      filename: file.name,
      mimeType: resolveMimeType(file),
      sizeBytes: file.size,
    };

    let presignResponse: PresignResponse;
    try {
      presignResponse = await firstValueFrom(
        this.http.post<PresignResponse>(`${this.baseUrl()}/presign`, presignRequest)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to get upload URL');
    }

    // Track pending upload
    const pendingUpload: PendingUpload = {
      file,
      uploadId: presignResponse.uploadId,
      status: 'uploading',
      progress: 0,
    };
    this.updatePendingUpload(presignResponse.uploadId, pendingUpload);

    try {
      // Step 2: Upload to S3
      await this.uploadToS3(presignResponse.presignedUrl, file, presignResponse.uploadId);

      // Update status
      this.updatePendingUpload(presignResponse.uploadId, {
        ...pendingUpload,
        status: 'completing',
        progress: 100,
      });

      // Step 3: Mark as complete
      const completeResponse = await this.completeUpload(presignResponse.uploadId);

      // Update to ready
      this.updatePendingUpload(presignResponse.uploadId, {
        ...pendingUpload,
        status: 'ready',
        progress: 100,
      });

      return completeResponse;
    } catch (err) {
      // Mark as error
      const errorMessage = err instanceof Error ? err.message : 'Upload failed';
      this.updatePendingUpload(presignResponse.uploadId, {
        ...pendingUpload,
        status: 'error',
        error: errorMessage,
      });
      throw err;
    }
  }

  /**
   * Upload multiple files for a session.
   *
   * @param sessionId - The session to associate files with
   * @param files - Array of files to upload
   * @returns Array of upload results (success or error for each)
   */
  async uploadFiles(
    sessionId: string,
    files: File[]
  ): Promise<Array<{ file: File; result?: CompleteUploadResponse; error?: Error }>> {
    // Limit number of files
    const filesToUpload = files.slice(0, MAX_FILES_PER_MESSAGE);

    // Upload in parallel
    const results = await Promise.all(
      filesToUpload.map(async (file) => {
        try {
          const result = await this.uploadFile(sessionId, file);
          return { file, result };
        } catch (error) {
          return { file, error: error as Error };
        }
      })
    );

    return results;
  }

  /**
   * Upload file content directly to S3 via pre-signed URL.
   */
  private async uploadToS3(presignedUrl: string, file: File, uploadId: string): Promise<void> {
    try {
      // Use XMLHttpRequest for progress tracking
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) {
            const progress = Math.round((event.loaded / event.total) * 100);
            const current = this._pendingUploads().get(uploadId);
            if (current) {
              this.updatePendingUpload(uploadId, { ...current, progress });
            }
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            reject(new FileUploadError(
              `S3 upload failed: ${xhr.status} ${xhr.statusText}`,
              'S3_UPLOAD_FAILED',
              { status: xhr.status }
            ));
          }
        };

        xhr.onerror = () => {
          reject(new FileUploadError(
            'Network error during S3 upload',
            'NETWORK_ERROR'
          ));
        };

        xhr.open('PUT', presignedUrl);
        xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
        xhr.send(file);
      });
    } catch (err) {
      if (err instanceof FileUploadError) throw err;
      throw new FileUploadError(
        'Failed to upload to S3',
        'S3_UPLOAD_FAILED',
        { originalError: String(err) }
      );
    }
  }

  /**
   * Mark an upload as complete.
   */
  async completeUpload(uploadId: string): Promise<CompleteUploadResponse> {
    try {
      return await firstValueFrom(
        this.http.post<CompleteUploadResponse>(`${this.baseUrl()}/${uploadId}/complete`, {})
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to complete upload');
    }
  }

  /**
   * Delete a file.
   */
  async deleteFile(uploadId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.delete(`${this.baseUrl()}/${uploadId}`)
      );

      // Remove from pending uploads
      this._pendingUploads.update(map => {
        const newMap = new Map(map);
        newMap.delete(uploadId);
        return newMap;
      });
    } catch (err) {
      throw this.handleApiError(err, 'Failed to delete file');
    }
  }

  /**
   * List files for a session.
   */
  async listSessionFiles(sessionId: string): Promise<FileMetadata[]> {
    try {
      const response = await firstValueFrom(
        this.http.get<FileListResponse>(`${this.baseUrl()}`, {
          params: { sessionId }
        })
      );
      return response.files;
    } catch (err) {
      throw this.handleApiError(err, 'Failed to list files');
    }
  }

  /**
   * List all files for the authenticated user.
   *
   * @param options - Pagination and sorting options
   * @returns FileListResponse with files and pagination
   */
  async listAllFiles(options?: {
    limit?: number;
    cursor?: string | null;
    sortBy?: 'date' | 'size' | 'type';
    sortOrder?: 'asc' | 'desc';
  }): Promise<FileListResponse> {
    try {
      const params: Record<string, string> = {};

      if (options?.limit) {
        params['limit'] = options.limit.toString();
      }
      if (options?.cursor) {
        params['cursor'] = options.cursor;
      }
      if (options?.sortBy) {
        params['sortBy'] = options.sortBy;
      }
      if (options?.sortOrder) {
        params['sortOrder'] = options.sortOrder;
      }

      const response = await firstValueFrom(
        this.http.get<FileListResponse>(`${this.baseUrl()}`, { params })
      );
      return response;
    } catch (err) {
      throw this.handleApiError(err, 'Failed to list files');
    }
  }

  /**
   * Delete multiple files.
   *
   * @param uploadIds - Array of upload IDs to delete
   * @returns Array of results with success/failure for each
   */
  async deleteFiles(uploadIds: string[]): Promise<Array<{ uploadId: string; success: boolean; error?: string }>> {
    const results: Array<{ uploadId: string; success: boolean; error?: string }> = [];

    for (const uploadId of uploadIds) {
      try {
        await this.deleteFile(uploadId);
        results.push({ uploadId, success: true });
      } catch (err) {
        const error = err instanceof Error ? err.message : 'Unknown error';
        results.push({ uploadId, success: false, error });
      }
    }

    return results;
  }

  /**
   * Fetch a short-lived presigned GET URL for a file.
   *
   * Used by the UI to render inline image previews and the lightbox.
   * The URL expires after a few minutes; refetch on expiry.
   */
  async getPreviewUrl(uploadId: string): Promise<PreviewUrlResponse> {
    try {
      return await firstValueFrom(
        this.http.get<PreviewUrlResponse>(`${this.baseUrl()}/${uploadId}/preview-url`)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to get preview URL');
    }
  }

  /**
   * Fetch a UTF-8 text snippet from the start of a file.
   *
   * Returns an empty snippet for non-text MIME types so the UI can fall
   * back to a skeleton mockup.
   */
  async getTextSnippet(uploadId: string): Promise<TextSnippetResponse> {
    try {
      return await firstValueFrom(
        this.http.get<TextSnippetResponse>(`${this.baseUrl()}/${uploadId}/text-snippet`)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to get text snippet');
    }
  }

  /**
   * Fetch a presigned URL for a PNG thumbnail of the file's first page.
   *
   * Backend lazy-renders on first call and caches the result, so subsequent
   * calls return instantly. Distinguishes between "this file type can never
   * have a thumbnail" (415 → `unsupported`) and "we tried but it didn't
   * work" (404/422/network → `unavailable`) so the UI can decide whether
   * to retry or give up.
   */
  async getThumbnail(uploadId: string): Promise<ThumbnailFetchResult> {
    try {
      const response = await firstValueFrom(
        this.http.get<ThumbnailResponse>(`${this.baseUrl()}/${uploadId}/thumbnail`)
      );
      return { status: 'ready', response };
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 415) {
        return { status: 'unsupported' };
      }
      return { status: 'unavailable' };
    }
  }

  /**
   * Get user's quota status.
   */
  async loadQuota(): Promise<QuotaResponse> {
    try {
      const response = await firstValueFrom(
        this.http.get<QuotaResponse>(`${this.baseUrl()}/quota`)
      );
      this._quota.set(response);
      return response;
    } catch (err) {
      throw this.handleApiError(err, 'Failed to load quota');
    }
  }

  /**
   * Clear a pending upload from tracking.
   */
  clearPendingUpload(uploadId: string): void {
    this._pendingUploads.update(map => {
      const newMap = new Map(map);
      newMap.delete(uploadId);
      return newMap;
    });
  }

  /**
   * Clear all completed uploads.
   */
  clearReadyUploads(): void {
    this._pendingUploads.update(map => {
      const newMap = new Map(map);
      for (const [id, upload] of newMap) {
        if (upload.status === 'ready') {
          newMap.delete(id);
        }
      }
      return newMap;
    });
  }

  /**
   * Clear all pending uploads.
   */
  clearAllPendingUploads(): void {
    this._pendingUploads.set(new Map());
  }

  /**
   * Update a pending upload's state.
   */
  private updatePendingUpload(uploadId: string, upload: PendingUpload): void {
    this._pendingUploads.update(map => {
      const newMap = new Map(map);
      newMap.set(uploadId, upload);
      return newMap;
    });
  }

  /**
   * Handle API errors with proper typing.
   */
  private handleApiError(err: unknown, fallbackMessage: string): FileUploadError {
    if (err instanceof HttpErrorResponse) {
      const body = err.error;

      // Check for quota exceeded
      if (body?.error === 'QUOTA_EXCEEDED') {
        return new QuotaExceededError(
          body.currentUsage,
          body.maxAllowed,
          body.requiredSpace
        );
      }

      // Check for invalid file type
      if (err.status === 400 && body?.detail?.includes('file type')) {
        return new InvalidFileTypeError(body.detail);
      }

      // Check for file too large
      if (err.status === 400 && body?.detail?.includes('too large')) {
        return new FileTooLargeError(0, MAX_FILE_SIZE_BYTES);
      }

      // Generic API error
      const message = body?.detail || body?.message || err.message || fallbackMessage;
      return new FileUploadError(message, 'API_ERROR', { status: err.status });
    }

    if (err instanceof FileUploadError) {
      return err;
    }

    return new FileUploadError(
      err instanceof Error ? err.message : fallbackMessage,
      'UNKNOWN_ERROR'
    );
  }
}
