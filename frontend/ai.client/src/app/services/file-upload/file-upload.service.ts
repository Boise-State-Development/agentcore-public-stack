import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AuthService } from '../../auth/auth.service';

/**
 * File status enum matching backend FileStatus
 */
export type FileStatus = 'pending' | 'ready' | 'failed';

/**
 * Allowed MIME types for file uploads (Bedrock-compliant)
 */
export const ALLOWED_MIME_TYPES: Record<string, string> = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'text/plain': 'txt',
  'text/html': 'html',
  'text/csv': 'csv',
  'application/vnd.ms-excel': 'xls',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
  'text/markdown': 'md',
};

/**
 * Allowed file extensions
 */
export const ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.txt', '.html', '.csv', '.xls', '.xlsx', '.md'];

/**
 * Maximum file size in bytes (4MB)
 */
export const MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024;

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
  private authService = inject(AuthService);

  private readonly baseUrl = `${environment.appApiUrl}/files`;

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
   * Validate a file before upload.
   * @throws FileUploadError if validation fails
   */
  validateFile(file: File): void {
    // Check size
    if (file.size > MAX_FILE_SIZE_BYTES) {
      throw new FileTooLargeError(file.size, MAX_FILE_SIZE_BYTES);
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

    await this.authService.ensureAuthenticated();
    this._error.set(null);

    // Step 1: Request pre-signed URL
    const presignRequest: PresignRequest = {
      sessionId,
      filename: file.name,
      mimeType: file.type || 'application/octet-stream',
      sizeBytes: file.size,
    };

    let presignResponse: PresignResponse;
    try {
      presignResponse = await firstValueFrom(
        this.http.post<PresignResponse>(`${this.baseUrl}/presign`, presignRequest)
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
    await this.authService.ensureAuthenticated();

    try {
      return await firstValueFrom(
        this.http.post<CompleteUploadResponse>(`${this.baseUrl}/${uploadId}/complete`, {})
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to complete upload');
    }
  }

  /**
   * Delete a file.
   */
  async deleteFile(uploadId: string): Promise<void> {
    await this.authService.ensureAuthenticated();

    try {
      await firstValueFrom(
        this.http.delete(`${this.baseUrl}/${uploadId}`)
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
    await this.authService.ensureAuthenticated();

    try {
      const response = await firstValueFrom(
        this.http.get<FileListResponse>(`${this.baseUrl}`, {
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
    await this.authService.ensureAuthenticated();

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
        this.http.get<FileListResponse>(`${this.baseUrl}`, { params })
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
   * Get user's quota status.
   */
  async loadQuota(): Promise<QuotaResponse> {
    await this.authService.ensureAuthenticated();

    try {
      const response = await firstValueFrom(
        this.http.get<QuotaResponse>(`${this.baseUrl}/quota`)
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
