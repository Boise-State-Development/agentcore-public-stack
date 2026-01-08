import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AuthService } from '../../auth/auth.service';
import {
  CreateDocumentRequest,
  UploadUrlResponse,
  Document,
  DocumentsListResponse
} from '../models/document.model';

/**
 * Error class for document upload operations
 */
export class DocumentUploadError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(message);
    this.name = 'DocumentUploadError';
  }
}

/**
 * Service for managing assistant document uploads via pre-signed URLs.
 *
 * Upload flow:
 * 1. Client calls requestUploadUrl() with file metadata
 * 2. Backend validates, creates document record, returns pre-signed URL
 * 3. Client uploads directly to S3 using pre-signed URL with progress tracking
 * 4. Document status is automatically updated by backend after upload
 */
@Injectable({
  providedIn: 'root'
})
export class DocumentService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);
  private readonly baseUrl = `${environment.appApiUrl}/assistants`;

  /**
   * Request a presigned URL for uploading a document to an assistant.
   *
   * @param assistantId - The assistant identifier
   * @param file - The file to upload
   * @returns Promise resolving to upload URL response
   * @throws DocumentUploadError on validation or API failure
   */
  async requestUploadUrl(assistantId: string, file: File): Promise<UploadUrlResponse> {
    await this.authService.ensureAuthenticated();

    const request: CreateDocumentRequest = {
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
      sizeBytes: file.size
    };

    try {
      return await firstValueFrom(
        this.http.post<UploadUrlResponse>(
          `${this.baseUrl}/${assistantId}/documents/upload-url`,
          request
        )
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to get upload URL');
    }
  }

  /**
   * Upload file content directly to S3 via pre-signed URL with progress tracking.
   *
   * @param presignedUrl - The presigned S3 URL
   * @param file - The file to upload
   * @param onProgress - Progress callback (0-100)
   * @returns Promise that resolves when upload completes
   * @throws DocumentUploadError on upload failure
   */
  async uploadToS3(
    presignedUrl: string,
    file: File,
    onProgress: (progress: number) => void
  ): Promise<void> {
    try {
      // Extract Content-Type from presigned URL if present
      // The presigned URL includes content-type in the query string
      const urlObj = new URL(presignedUrl);
      const contentTypeFromUrl = urlObj.searchParams.get('content-type');
      // Use Content-Type from URL (what was used to sign) or fall back to file type
      const contentType = contentTypeFromUrl 
        ? decodeURIComponent(contentTypeFromUrl)
        : (file.type || 'application/octet-stream');

      // Use XMLHttpRequest for progress tracking
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) {
            const progress = Math.round((event.loaded / event.total) * 100);
            onProgress(progress);
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            // Try to parse error response from S3
            let errorMessage = `S3 upload failed: ${xhr.status} ${xhr.statusText}`;
            try {
              const responseText = xhr.responseText;
              if (responseText) {
                // S3 errors are usually XML
                const parser = new DOMParser();
                const xmlDoc = parser.parseFromString(responseText, 'text/xml');
                const codeElement = xmlDoc.querySelector('Code');
                const messageElement = xmlDoc.querySelector('Message');
                if (codeElement?.textContent) {
                  errorMessage = `${codeElement.textContent}: ${messageElement?.textContent || xhr.statusText}`;
                }
              }
            } catch (parseError) {
              // If parsing fails, use the response text or default message
              if (xhr.responseText) {
                errorMessage = `S3 upload failed: ${xhr.responseText}`;
              }
            }
            reject(new DocumentUploadError(
              errorMessage,
              'S3_UPLOAD_FAILED',
              { 
                status: xhr.status,
                statusText: xhr.statusText,
                responseText: xhr.responseText
              }
            ));
          }
        };

        xhr.onerror = () => {
          reject(new DocumentUploadError(
            'Network error during S3 upload',
            'NETWORK_ERROR'
          ));
        };

        xhr.open('PUT', presignedUrl);
        // Use the Content-Type that matches what was used to sign the URL
        xhr.setRequestHeader('Content-Type', contentType);
        xhr.send(file);
      });
    } catch (err) {
      if (err instanceof DocumentUploadError) throw err;
      throw new DocumentUploadError(
        'Failed to upload to S3',
        'S3_UPLOAD_FAILED',
        { originalError: String(err) }
      );
    }
  }

  /**
   * List all documents for an assistant.
   *
   * @param assistantId - The assistant identifier
   * @param limit - Optional limit on number of documents
   * @param nextToken - Optional pagination token
   * @returns Promise resolving to list of documents
   * @throws DocumentUploadError on API failure
   */
  async listDocuments(
    assistantId: string,
    limit?: number,
    nextToken?: string
  ): Promise<DocumentsListResponse> {
    await this.authService.ensureAuthenticated();

    try {
      let url = `${this.baseUrl}/${assistantId}/documents`;
      const params: string[] = [];
      
      if (limit !== undefined) {
        params.push(`limit=${limit}`);
      }
      if (nextToken) {
        params.push(`next_token=${encodeURIComponent(nextToken)}`);
      }
      
      if (params.length > 0) {
        url += `?${params.join('&')}`;
      }

      return await firstValueFrom(
        this.http.get<DocumentsListResponse>(url)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to list documents');
    }
  }

  /**
   * Get a specific document by ID.
   *
   * @param assistantId - The assistant identifier
   * @param documentId - The document identifier
   * @returns Promise resolving to document
   * @throws DocumentUploadError on API failure
   */
  async getDocument(assistantId: string, documentId: string): Promise<Document> {
    await this.authService.ensureAuthenticated();

    try {
      return await firstValueFrom(
        this.http.get<Document>(`${this.baseUrl}/${assistantId}/documents/${documentId}`)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to get document');
    }
  }

  /**
   * Delete a document.
   *
   * @param assistantId - The assistant identifier
   * @param documentId - The document identifier
   * @returns Promise that resolves when deletion completes
   * @throws DocumentUploadError on API failure
   */
  async deleteDocument(assistantId: string, documentId: string): Promise<void> {
    await this.authService.ensureAuthenticated();

    try {
      await firstValueFrom(
        this.http.delete<void>(`${this.baseUrl}/${assistantId}/documents/${documentId}`)
      );
    } catch (err) {
      throw this.handleApiError(err, 'Failed to delete document');
    }
  }

  /**
   * Poll document status until it reaches a terminal state (complete or failed).
   * Uses exponential backoff with a maximum interval.
   *
   * @param assistantId - The assistant identifier
   * @param documentId - The document identifier
   * @param onStatusUpdate - Callback called each time status is checked
   * @param maxPollTime - Maximum time to poll in milliseconds (default: 5 minutes)
   * @param initialInterval - Initial polling interval in milliseconds (default: 2 seconds)
   * @param maxInterval - Maximum polling interval in milliseconds (default: 10 seconds)
   * @returns Promise resolving to final document state
   * @throws DocumentUploadError if polling times out or fails
   */
  async pollDocumentStatus(
    assistantId: string,
    documentId: string,
    onStatusUpdate?: (document: Document) => void,
    maxPollTime: number = 5 * 60 * 1000, // 5 minutes
    initialInterval: number = 500, // 500ms - start fast to catch quick status changes
    maxInterval: number = 10000 // 10 seconds
  ): Promise<Document> {
    const startTime = Date.now();
    let currentInterval = initialInterval;
    const terminalStates: Array<'complete' | 'failed'> = ['complete', 'failed'];
    let pollCount = 0;

    while (Date.now() - startTime < maxPollTime) {
      try {
        const document = await this.getDocument(assistantId, documentId);
        
        // Call update callback
        if (onStatusUpdate) {
          onStatusUpdate(document);
        }

        // Check if we've reached a terminal state
        if (terminalStates.includes(document.status as 'complete' | 'failed')) {
          return document;
        }

        pollCount++;
        
        // For the first few polls, use shorter intervals to catch quick status changes
        // After 5 polls, switch to exponential backoff
        if (pollCount < 5) {
          currentInterval = initialInterval;
        } else {
          // Increase interval for next poll (exponential backoff, capped at maxInterval)
          currentInterval = Math.min(currentInterval * 1.5, maxInterval);
        }
        
        // Wait before next poll
        await new Promise(resolve => setTimeout(resolve, currentInterval));
      } catch (err) {
        // If it's a 404, the document might not exist yet, continue polling
        if (err instanceof DocumentUploadError && err.code === 'HTTP_404') {
          await new Promise(resolve => setTimeout(resolve, currentInterval));
          continue;
        }
        throw err;
      }
    }

    // Timeout - get final status
    const finalDocument = await this.getDocument(assistantId, documentId);
    if (onStatusUpdate) {
      onStatusUpdate(finalDocument);
    }
    
    throw new DocumentUploadError(
      `Document processing timed out after ${maxPollTime / 1000}s. Current status: ${finalDocument.status}`,
      'POLL_TIMEOUT',
      { documentId, finalStatus: finalDocument.status }
    );
  }

  /**
   * Handle API errors and convert to DocumentUploadError.
   */
  private handleApiError(err: unknown, defaultMessage: string): DocumentUploadError {
    if (err instanceof HttpErrorResponse) {
      const status = err.status;
      const message = err.error?.detail || err.error?.message || err.message || defaultMessage;
      
      return new DocumentUploadError(
        message,
        `HTTP_${status}`,
        { status, error: err.error }
      );
    }
    
    if (err instanceof Error) {
      return new DocumentUploadError(
        err.message || defaultMessage,
        'UNKNOWN_ERROR',
        { originalError: err }
      );
    }
    
    return new DocumentUploadError(
      defaultMessage,
      'UNKNOWN_ERROR',
      { originalError: String(err) }
    );
  }
}

