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
  resolveMimeType,
  FileTooLargeError,
  InvalidFileTypeError,
  QuotaExceededError,
  MAX_FILE_SIZE_BYTES,
  PPTX_MAX_FILE_SIZE_BYTES,
  LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES,
  maxFileSizeFor,
  isReferenceDocument,
  isAttachableSourceFile,
  resolveMimeTypeFor,
  ALLOWED_EXTENSIONS
} from './file-upload.service';
import { ConfigService } from '../config.service';
import { FEATURES } from '../features';

const FLAG_OFF = { largeDocumentAttachments: false };
const FLAG_ON = { largeDocumentAttachments: true };

/** A File that reports `size` without allocating it (a 50 MB string is slow). */
function sizedFile(name: string, type: string, size: number): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

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
        { provide: ConfigService, useValue: mockConfigService },
        { provide: FEATURES, useValue: FLAG_OFF }
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

      // A deck never goes inline to Bedrock, so the 4MB inline-document
      // ceiling doesn't bound it. Corporate templates with imagery clear
      // 4MB routinely — capping them there is what broke `template_name`.
      it('should allow a .pptx above the general cap', () => {
        const file = new File(['x'.repeat(MAX_FILE_SIZE_BYTES + 1)], 'deck.pptx', {
          type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        });
        expect(() => service.validateFile(file)).not.toThrow();
      });

      it('should reject a large PDF while large-document attachments are off', () => {
        const file = sizedFile('manual.pdf', 'application/pdf', 38 * 1024 * 1024);
        expect(() => service.validateFile(file)).toThrow(FileTooLargeError);
      });

      it('should accept a large PDF and name the real ceiling when the flag is on', () => {
        TestBed.resetTestingModule();
        TestBed.configureTestingModule({
          providers: [
            provideHttpClient(),
            provideHttpClientTesting(),
            { provide: ConfigService, useValue: mockConfigService },
            { provide: FEATURES, useValue: FLAG_ON }
          ]
        });
        const flagged = TestBed.inject(FileUploadService);
        httpMock = TestBed.inject(HttpTestingController);

        expect(() =>
          flagged.validateFile(sizedFile('manual.pdf', 'application/pdf', 38 * 1024 * 1024))
        ).not.toThrow();
        expect(() =>
          flagged.validateFile(sizedFile('manual.pdf', 'application/pdf', 51 * 1024 * 1024))
        ).toThrow('Maximum: 50 MB');
      });

      it('should still reject a .pptx above the presentation cap', () => {
        const file = new File(['x'.repeat(PPTX_MAX_FILE_SIZE_BYTES + 1)], 'huge.pptx', {
          type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
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

      // .pptx uploads feed the PowerPoint tools (read a deck, or pass one as
      // a template to create_powerpoint_presentation). The backend has always
      // allowed the MIME type; this list was the only thing blocking it, and
      // the create-deck tool's error text tells users to upload a template.
      it('should allow .pptx by MIME type', () => {
        const file = new File(['content'], 'deck.pptx', {
          type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        });
        expect(() => service.validateFile(file)).not.toThrow();
      });

      it('should allow .pptx by extension when MIME is empty', () => {
        const file = new File(['content'], 'deck.pptx', { type: '' });
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

      it('answers for a registered file that never uploaded from this browser', () => {
        const meta = {
          uploadId: 'imported-1', filename: 'Notes.docx', mimeType: 'application/x', sizeBytes: 9,
          sessionId: 's1', s3Uri: 's3://b/k', status: 'ready' as const, createdAt: '',
        };
        service.registerKnownFile(meta);
        expect(service.getReadyFileById('imported-1')).toEqual(meta);
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

      describe('maxFileSizeFor', () => {
        it('should return the general cap for ordinary files', () => {
          const file = new File(['c'], 'doc.pdf', { type: 'application/pdf' });
          expect(maxFileSizeFor(file, FLAG_OFF)).toBe(MAX_FILE_SIZE_BYTES);
        });

        it('should return the presentation cap by MIME type', () => {
          const file = new File(['c'], 'deck.pptx', {
            type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
          });
          expect(maxFileSizeFor(file, FLAG_OFF)).toBe(PPTX_MAX_FILE_SIZE_BYTES);
        });

        it('should return the presentation cap by extension when MIME is empty', () => {
          const file = new File(['c'], 'deck.pptx', { type: '' });
          expect(maxFileSizeFor(file, FLAG_OFF)).toBe(PPTX_MAX_FILE_SIZE_BYTES);
        });

        it('should return the large-document ceiling for every document class when the flag is on', () => {
          const documents: [string, string][] = [
            ['manual.pdf', 'application/pdf'],
            ['handbook.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
            ['log.txt', 'text/plain'],
            ['notes.md', 'text/markdown'],
            ['page.html', 'text/html'],
            // The browser reported nothing; the extension decides, as presign will.
            ['scan.pdf', ''],
          ];
          for (const [name, type] of documents) {
            expect(maxFileSizeFor(new File(['c'], name, { type }), FLAG_ON)).toBe(
              LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES
            );
          }
        });

        it('should keep spreadsheets, images and decks on their own caps when the flag is on', () => {
          expect(maxFileSizeFor(new File(['c'], 'data.csv', { type: 'text/csv' }), FLAG_ON)).toBe(
            MAX_FILE_SIZE_BYTES
          );
          expect(
            maxFileSizeFor(
              new File(['c'], 'sales.xlsx', {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
              }),
              FLAG_ON
            )
          ).toBe(MAX_FILE_SIZE_BYTES);
          expect(maxFileSizeFor(new File(['c'], 'photo.png', { type: 'image/png' }), FLAG_ON)).toBe(
            MAX_FILE_SIZE_BYTES
          );
          expect(maxFileSizeFor(new File(['c'], 'deck.pptx', { type: '' }), FLAG_ON)).toBe(
            PPTX_MAX_FILE_SIZE_BYTES
          );
        });

        it('should not exceed the backend large-document ceiling', () => {
          // LARGE_DOCUMENT_MAX_BYTES in backend/src/apis/shared/files/models.py.
          expect(LARGE_DOCUMENT_MAX_FILE_SIZE_BYTES).toBe(50 * 1024 * 1024);
        });
      });

      describe('isAttachableSourceFile (a file-source listing entry)', () => {
        const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

        it('accepts a type the chat takes, within its cap', () => {
          expect(isAttachableSourceFile({ name: 'a.pdf', mimeType: 'application/pdf', sizeBytes: 1024 }, FLAG_OFF)).toBe(true);
        });

        it('judges a provider-native document on what it downloads as', () => {
          const gdoc = { name: 'Notes', mimeType: 'application/vnd.google-apps.document', downloadMimeType: DOCX, sizeBytes: null };
          expect(isAttachableSourceFile(gdoc, FLAG_OFF)).toBe(true);
          const form = { name: 'Survey', mimeType: 'application/vnd.google-apps.form', sizeBytes: null };
          expect(isAttachableSourceFile(form, FLAG_OFF)).toBe(false);
        });

        it('refuses a type the chat does not take', () => {
          expect(isAttachableSourceFile({ name: 'a.zip', mimeType: 'application/zip', sizeBytes: 10 }, FLAG_OFF)).toBe(false);
        });

        it('falls back to the extension when the provider reports a vague type', () => {
          expect(isAttachableSourceFile({ name: 'notes.md', mimeType: 'application/octet-stream', sizeBytes: 10 }, FLAG_OFF)).toBe(true);
          expect(resolveMimeTypeFor('notes.md', 'application/octet-stream')).toBe('text/markdown');
        });

        it('applies the class cap, which the large-document flag raises for documents', () => {
          const big = { name: 'scan.pdf', mimeType: 'application/pdf', sizeBytes: 10 * 1024 * 1024 };
          expect(isAttachableSourceFile(big, FLAG_OFF)).toBe(false);
          expect(isAttachableSourceFile(big, FLAG_ON)).toBe(true);
          const deck = { name: 'deck.pptx', mimeType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', sizeBytes: 10 * 1024 * 1024 };
          expect(isAttachableSourceFile(deck, FLAG_OFF)).toBe(true);
        });

        it('lets an unknown size through for the backend to judge', () => {
          expect(isAttachableSourceFile({ name: 'a.pdf', mimeType: 'application/pdf' }, FLAG_OFF)).toBe(true);
        });
      });

      describe('isReferenceDocument', () => {
        const MB = 1024 * 1024;

        it('should be a document over the inline limit and within the ceiling', () => {
          expect(isReferenceDocument('application/pdf', 4 * MB)).toBe(false);
          expect(isReferenceDocument('application/pdf', 4 * MB + 1)).toBe(true);
          expect(isReferenceDocument('application/pdf', 50 * MB)).toBe(true);
          expect(isReferenceDocument('application/pdf', 50 * MB + 1)).toBe(false);
        });

        it('should never be a spreadsheet, deck or image', () => {
          expect(isReferenceDocument('text/csv', 10 * MB)).toBe(false);
          expect(
            isReferenceDocument(
              'application/vnd.openxmlformats-officedocument.presentationml.presentation',
              10 * MB
            )
          ).toBe(false);
          expect(isReferenceDocument('image/png', 10 * MB)).toBe(false);
        });
      });

      describe('maxFileSizeFor (presentation cap)', () => {

        it('should not exceed the backend presentation cap', () => {
          // The backend is the enforcing side; if the UI cap were larger it
          // would accept decks that presign then rejects with a 400.
          expect(PPTX_MAX_FILE_SIZE_BYTES).toBe(25 * 1024 * 1024);
        });
      });

      describe('resolveMimeType', () => {
        it('should keep an allowed MIME the browser reported', () => {
          const file = new File(['c'], 'doc.pdf', { type: 'application/pdf' });
          expect(resolveMimeType(file)).toBe('application/pdf');
        });

        it('should resolve .pptx from the extension when MIME is empty', () => {
          // Presign would otherwise receive application/octet-stream, which
          // the backend allowlist rejects — and a deck stored under the wrong
          // MIME is invisible to read_powerpoint_presentation, which matches
          // the stored type exactly.
          const file = new File(['c'], 'deck.pptx', { type: '' });
          expect(resolveMimeType(file)).toBe(
            'application/vnd.openxmlformats-officedocument.presentationml.presentation'
          );
        });

        it('should override a generic octet-stream MIME using the extension', () => {
          const file = new File(['c'], 'data.xlsx', { type: 'application/octet-stream' });
          expect(resolveMimeType(file)).toBe(
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
          );
        });

        it('should pass through an unknown type it cannot resolve', () => {
          // validateFile is the gate for rejection; this helper must not
          // invent a MIME for something the allowlist never accepted.
          const file = new File(['c'], 'thing.exe', { type: 'application/exe' });
          expect(resolveMimeType(file)).toBe('application/exe');
        });
      });

      describe('getAcceptedFileTypes equivalent', () => {
        it('should return allowed extensions', () => {
          expect(ALLOWED_EXTENSIONS).toContain('.pdf');
          expect(ALLOWED_EXTENSIONS).toContain('.png');
          expect(ALLOWED_EXTENSIONS).toContain('.docx');
          // Drives the file picker's `accept` filter in chat-input.
          expect(ALLOWED_EXTENSIONS).toContain('.pptx');
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