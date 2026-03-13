import { Injectable } from '@angular/core';

/**
 * Service for uploading files to S3 via presigned URLs with progress tracking.
 * Follows the pattern from DocumentService.uploadToS3() in assistants/services/document.service.ts.
 */
@Injectable({
  providedIn: 'root',
})
export class FineTuningUploadService {
  /**
   * Upload a file directly to S3 using a presigned PUT URL.
   *
   * @param presignedUrl - The presigned S3 URL from the backend
   * @param file - The file to upload
   * @param onProgress - Callback with upload progress (0-100)
   * @returns Promise that resolves when upload completes
   * @throws Error on upload failure
   */
  async uploadFile(
    presignedUrl: string,
    file: File,
    onProgress: (progress: number) => void,
  ): Promise<void> {
    // Extract Content-Type from presigned URL query params (what was used to sign)
    const urlObj = new URL(presignedUrl);
    const contentTypeFromUrl = urlObj.searchParams.get('content-type');
    const contentType = contentTypeFromUrl
      ? decodeURIComponent(contentTypeFromUrl)
      : file.type || 'application/octet-stream';

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
          let errorMessage = `S3 upload failed: ${xhr.status} ${xhr.statusText}`;
          try {
            const responseText = xhr.responseText;
            if (responseText) {
              const parser = new DOMParser();
              const xmlDoc = parser.parseFromString(responseText, 'text/xml');
              const codeElement = xmlDoc.querySelector('Code');
              const messageElement = xmlDoc.querySelector('Message');
              if (codeElement?.textContent) {
                errorMessage = `${codeElement.textContent}: ${messageElement?.textContent || xhr.statusText}`;
              }
            }
          } catch {
            if (xhr.responseText) {
              errorMessage = `S3 upload failed: ${xhr.responseText}`;
            }
          }
          reject(new Error(errorMessage));
        }
      };

      xhr.onerror = () => {
        reject(new Error('Network error during file upload'));
      };

      xhr.open('PUT', presignedUrl);
      xhr.setRequestHeader('Content-Type', contentType);
      xhr.send(file);
    });
  }
}
