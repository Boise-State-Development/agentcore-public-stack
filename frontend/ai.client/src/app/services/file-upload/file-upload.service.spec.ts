import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { 
  FileUploadService, 
  formatBytes, 
  isAllowedMimeType, 
  getFileExtension,
  FileTooLargeError,
  InvalidFileTypeError,
  QuotaExceededError,
  MAX_FILE_SIZE_BYTES,
  ALLOWED_EXTENSIONS
} from './file-upload.service';
import { ConfigService } from '../config.service';

describe('FileUploadService', () => {
  let service: FileUploadService;
  let httpMock: HttpTestingController;
  let mockConfigService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockConfigService = {
      appApiUrl: signal('http://localhost:8000')
    };

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: mockConfigService }
      ]
    });

    service = TestBed.inject(FileUploadService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) {
        req.flush({});
      }
    });
    httpMock?.verify();
    TestBed.resetTestingModule();
  });

  describe('formatBytes', () => {
    it('should format 0 bytes', () => {
      expect(formatBytes(0)).toBe('0 B');
    });

    it('should format 1024 bytes as KB', () => {
      expect(formatBytes(1024)).toBe('1 KB');
    });

    it('should format 1048576 bytes as MB', () => {
      expect(formatBytes(1048576)).toBe('1 MB');
    });

    it('should format 4194304 bytes as MB', () => {
      expect(formatBytes(4194304)).toBe('4 MB');
    });
  });

  describe('isAllowedMimeType', () => {
    it('should return true for valid MIME types', () => {
      expect(isAllowedMimeType('application/pdf')).toBe(true);
      expect(isAllowedMimeType('image/png')).toBe(true);
      expect(isAllowedMimeType('text/plain')).toBe(true);
    });

    it('should return false for invalid MIME types', () => {
      expect(isAllowedMimeType('application/exe')).toBe(false);
      expect(isAllowedMimeType('video/mp4')).toBe(false);
      expect(isAllowedMimeType('')).toBe(false);
    });
  });

  describe('getFileExtension', () => {
    it('should extract extension with dot', () => {
      expect(getFileExtension('file.pdf')).toBe('.pdf');
      expect(getFileExtension('document.docx')).toBe('.docx');
    });

    it('should return empty string without extension', () => {
      expect(getFileExtension('filename')).toBe('');
    });

    it('should handle multiple dots', () => {
      expect(getFileExtension('file.name.pdf')).toBe('.pdf');
      expect(getFileExtension('archive.tar.gz')).toBe('.gz');
    });
  });

  describe('Error Classes', () => {
    describe('FileTooLargeError', () => {
      it('should create error with correct message and details', () => {
        const error = new FileTooLargeError(5000000, 4000000);
        expect(error.message).toContain('File too large');
        expect(error.code).toBe('FILE_TOO_LARGE');
        expect(error.details).toEqual({ sizeBytes: 5000000, maxSize: 4000000 });
      });
    });

    describe('InvalidFileTypeError', () => {
      it('should create error with correct message and details', () => {
        const error = new InvalidFileTypeError('application/exe');
        expect(error.message).toContain('Invalid file type');
        expect(error.code).toBe('INVALID_FILE_TYPE');
        expect(error.details?.['mimeType']).toBe('application/exe');
      });
    });

    describe('QuotaExceededError', () => {
      it('should create error with correct message and details', () => {
        const error = new QuotaExceededError(1000, 2000, 500);
        expect(error.message).toContain('Storage quota exceeded');
        expect(error.code).toBe('QUOTA_EXCEEDED');
        expect(error.details).toEqual({ 
          currentUsage: 1000, 
          maxAllowed: 2000, 
          requiredSpace: 500 
        });
      });
    });
  });

  describe('FileUploadService', () => {
    describe('validateFile', () => {
      it('should pass validation for valid file', () => {
        const file = new File(['content'], 'test.pdf', { type: 'application/pdf' });
        expect(() => service.validateFile(file)).not.toThrow();
      });

      it('should throw FileTooLargeError for oversized file', () => {
        const file = new File(['x'.repeat(MAX_FILE_SIZE_BYTES + 1)], 'large.pdf', { 
          type: 'application/pdf' 
        });
        expect(() => service.validateFile(file)).toThrow(FileTooLargeError);
      });

      it('should throw InvalidFileTypeError for invalid MIME type', () => {
        const file = new File(['content'], 'test.exe', { type: 'application/exe' });
        expect(() => service.validateFile(file)).toThrow(InvalidFileTypeError);
      });

      it('should allow unknown MIME with valid extension', () => {
        const file = new File(['content'], 'test.pdf', { type: '' });
        expect(() => service.validateFile(file)).not.toThrow();
      });
    });

    describe('clearPendingUpload', () => {
      it('should remove upload from pending list', () => {
        service['_pendingUploads'].set(new Map([
          ['upload1', { uploadId: 'upload1' } as any]
        ]));
        
        service.clearPendingUpload('upload1');
        
        expect(service.pendingUploads().has('upload1')).toBe(false);
      });
    });

    describe('clearReadyUploads', () => {
      it('should remove only ready uploads', () => {
        service['_pendingUploads'].set(new Map([
          ['upload1', { uploadId: 'upload1', status: 'ready' } as any],
          ['upload2', { uploadId: 'upload2', status: 'uploading' } as any]
        ]));
        
        service.clearReadyUploads();
        
        expect(service.pendingUploads().has('upload1')).toBe(false);
        expect(service.pendingUploads().has('upload2')).toBe(true);
      });
    });

    describe('clearAllPendingUploads', () => {
      it('should clear all pending uploads', () => {
        service['_pendingUploads'].set(new Map([
          ['upload1', { uploadId: 'upload1' } as any],
          ['upload2', { uploadId: 'upload2' } as any]
        ]));
        
        service.clearAllPendingUploads();
        
        expect(service.pendingUploads().size).toBe(0);
      });
    });

    describe('computed signals', () => {
      it('should compute pendingUploadsList', () => {
        const upload1 = { uploadId: 'upload1', status: 'ready' } as any;
        const upload2 = { uploadId: 'upload2', status: 'uploading' } as any;
        
        service['_pendingUploads'].set(new Map([
          ['upload1', upload1],
          ['upload2', upload2]
        ]));
        
        const list = service.pendingUploadsList();
        expect(list).toHaveLength(2);
        expect(list).toContain(upload1);
        expect(list).toContain(upload2);
      });

      it('should compute hasActivePendingUploads', () => {
        service['_pendingUploads'].set(new Map([
          ['upload1', { status: 'uploading' } as any]
        ]));
        expect(service.hasActivePendingUploads()).toBe(true);

        service['_pendingUploads'].set(new Map([
          ['upload1', { status: 'ready' } as any]
        ]));
        expect(service.hasActivePendingUploads()).toBe(false);
      });

      it('should compute quotaUsagePercent', () => {
        service['_quota'].set({ usedBytes: 50, maxBytes: 100, fileCount: 1 });
        expect(service.quotaUsagePercent()).toBe(50);

        service['_quota'].set({ usedBytes: 150, maxBytes: 100, fileCount: 1 });
        expect(service.quotaUsagePercent()).toBe(100);

        service['_quota'].set(null);
        expect(service.quotaUsagePercent()).toBe(0);
      });
    });

    describe('HTTP methods', () => {
      describe('uploadFile', () => {
        it('should validate file before upload', () => {
          const badFile = new File(['x'], 'test.exe', { type: 'application/exe' });
          expect(service.uploadFile('session1', badFile)).rejects.toThrow(InvalidFileTypeError);
        });
      });

      describe('uploadFiles', () => {
        it('should upload multiple files and return results', async () => {
          const file1 = new File(['content1'], 'test1.pdf', { type: 'application/pdf' });
          const file2 = new File(['content2'], 'test2.txt', { type: 'text/plain' });
          
          const uploadPromise = service.uploadFiles('session1', [file1, file2]);

          await vi.waitFor(() => {
            const reqs = httpMock.match(() => true);
            expect(reqs.length).toBeGreaterThan(0);
            reqs.forEach(req => req.flush({}));
          });

          const results = await uploadPromise;
          expect(results).toHaveLength(2);
        });

      });

      describe('listSessionFiles', () => {
        it('should list files for session', async () => {
          const listPromise = service.listSessionFiles('session1');

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files?sessionId=session1');
            req.flush({ files: [{ uploadId: 'upload1', filename: 'test.pdf' }] });
          });

          const result = await listPromise;
          expect(result).toEqual([{ uploadId: 'upload1', filename: 'test.pdf' }]);
        });

        it('should handle API errors', async () => {
          const listPromise = service.listSessionFiles('session1');

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files?sessionId=session1');
            req.flush({ error: 'Not found' }, { status: 404, statusText: 'Not Found' });
          });

          await expect(listPromise).rejects.toThrow();
        });
      });

      describe('listAllFiles', () => {
        it('should list all files with default options', async () => {
          const listPromise = service.listAllFiles();

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files');
            req.flush({ files: [], nextCursor: null, totalCount: 0 });
          });

          const result = await listPromise;
          expect(result.files).toEqual([]);
        });

        it('should list files with pagination options', async () => {
          const listPromise = service.listAllFiles({ 
            limit: 10, 
            cursor: 'cursor123',
            sortBy: 'date',
            sortOrder: 'desc'
          });

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files?limit=10&cursor=cursor123&sortBy=date&sortOrder=desc');
            req.flush({ files: [], nextCursor: null, totalCount: 0 });
          });

          await listPromise;
        });
      });

      describe('deleteFile', () => {
        it('should delete file and remove from pending', async () => {
          service['_pendingUploads'].set(new Map([
            ['upload1', { uploadId: 'upload1' } as any]
          ]));

          const deletePromise = service.deleteFile('upload1');

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files/upload1');
            expect(req.request.method).toBe('DELETE');
            req.flush({});
          });

          await deletePromise;
          expect(service.pendingUploads().has('upload1')).toBe(false);
        });
      });

      describe('completeUpload', () => {
        it('should complete upload', async () => {
          const completePromise = service.completeUpload('upload1');

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files/upload1/complete');
            expect(req.request.method).toBe('POST');
            req.flush({ uploadId: 'upload1', status: 'ready' });
          });

          const result = await completePromise;
          expect(result.uploadId).toBe('upload1');
        });
      });

      describe('loadQuota', () => {
        it('should load and set quota', async () => {
          const quotaPromise = service.loadQuota();

          await vi.waitFor(() => {
            const req = httpMock.expectOne('http://localhost:8000/files/quota');
            req.flush({ usedBytes: 100, maxBytes: 1000, fileCount: 5 });
          });

          const result = await quotaPromise;
          expect(result).toEqual({ usedBytes: 100, maxBytes: 1000, fileCount: 5 });
          expect(service.quota()).toEqual({ usedBytes: 100, maxBytes: 1000, fileCount: 5 });
        });
      });
    });

    describe('getReadyFileById', () => {
      it('should return file metadata for ready upload', () => {
        const file = new File(['content'], 'test.pdf', { type: 'application/pdf' });
        service['_pendingUploads'].set(new Map([
          ['upload1', { uploadId: 'upload1', file, status: 'ready' } as any]
        ]));

        const result = service.getReadyFileById('upload1');
        expect(result).toBeDefined();
        expect(result?.uploadId).toBe('upload1');
        expect(result?.filename).toBe('test.pdf');
      });

      it('should return null for non-ready upload', () => {
        service['_pendingUploads'].set(new Map([
          ['upload1', { uploadId: 'upload1', status: 'uploading' } as any]
        ]));

        const result = service.getReadyFileById('upload1');
        expect(result).toBeNull();
      });

      it('should return null for non-existent upload', () => {
        const result = service.getReadyFileById('nonexistent');
        expect(result).toBeNull();
      });
    });

    describe('utility functions', () => {
      describe('formatBytes with decimals', () => {
        it('should format with custom decimals', () => {
          expect(formatBytes(1536)).toBe('1.5 KB');
          expect(formatBytes(2097152)).toBe('2 MB');
        });
      });

      describe('getFileIcon equivalent (getFileExtension)', () => {
        it('should handle various file extensions', () => {
          expect(getFileExtension('document.PDF')).toBe('.pdf');
          expect(getFileExtension('image.JPEG')).toBe('.jpeg');
          expect(getFileExtension('file')).toBe('');
        });
      });

      describe('isImageFile equivalent', () => {
        it('should identify image MIME types', () => {
          expect(isAllowedMimeType('image/png')).toBe(true);
          expect(isAllowedMimeType('image/jpeg')).toBe(true);
          expect(isAllowedMimeType('image/gif')).toBe(true);
          expect(isAllowedMimeType('image/webp')).toBe(true);
          expect(isAllowedMimeType('text/plain')).toBe(true);
        });
      });

      describe('isDocumentFile equivalent', () => {
        it('should identify document MIME types', () => {
          expect(isAllowedMimeType('application/pdf')).toBe(true);
          expect(isAllowedMimeType('application/vnd.openxmlformats-officedocument.wordprocessingml.document')).toBe(true);
          expect(isAllowedMimeType('text/plain')).toBe(true);
          expect(isAllowedMimeType('text/csv')).toBe(true);
        });
      });

      describe('getAcceptedFileTypes equivalent', () => {
        it('should return allowed extensions', () => {
          expect(ALLOWED_EXTENSIONS).toContain('.pdf');
          expect(ALLOWED_EXTENSIONS).toContain('.png');
          expect(ALLOWED_EXTENSIONS).toContain('.docx');
          expect(ALLOWED_EXTENSIONS.length).toBeGreaterThan(0);
        });
      });

      describe('getMaxFileSize equivalent', () => {
        it('should return max file size', () => {
          expect(MAX_FILE_SIZE_BYTES).toBe(4 * 1024 * 1024);
        });
      });
    });
  });
});