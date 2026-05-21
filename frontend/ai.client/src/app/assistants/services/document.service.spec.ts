import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { DocumentService, DocumentUploadError } from './document.service';
import { ConfigService } from '../../services/config.service';
describe('DocumentService', () => {
  let service: DocumentService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        DocumentService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(DocumentService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should request upload URL', async () => {
    const file = new File(['test'], 'test.txt', { type: 'text/plain' });
    const mockResponse = { uploadUrl: 'https://s3.example.com/upload', documentId: '1' };
    
    const promise = service.requestUploadUrl('assistant1', file);
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/upload-url').flush(mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should list documents', async () => {
    const mockResponse = { documents: [], nextToken: null };
    
    const promise = service.listDocuments('assistant1');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents').flush(mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get document', async () => {
    const mockDocument = { id: '1', filename: 'test.txt' };
    
    const promise = service.getDocument('assistant1', 'doc1');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/doc1').flush(mockDocument);
    });

    const result = await promise;
    expect(result).toEqual(mockDocument);
  });

  it('should delete document', async () => {
    const promise = service.deleteDocument('assistant1', 'doc1');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/doc1').flush(null);
    });

    await promise;
  });

  it('should get download URL successfully', async () => {
    const mockResponse = { downloadUrl: 'https://example.com/download' };
    
    const promise = service.getDownloadUrl('assistant1', 'doc-123');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/doc-123/download').flush(mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should list documents with limit and nextToken params', async () => {
    const mockResponse = { documents: [], nextToken: 'token-456' };
    
    const promise = service.listDocuments('assistant1', 10, 'token-123');
    
    await vi.waitFor(() => {
      httpMock.expectOne(r => r.url.includes('/assistants/assistant1/documents') && r.url.includes('limit=10')).flush(mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should handle requestUploadUrl error', async () => {
    const file = new File(['test'], 'test.txt', { type: 'text/plain' });
    const promise = service.requestUploadUrl('assistant1', file);
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/upload-url').flush({ detail: 'File too large' }, { status: 413, statusText: 'Payload Too Large' });
    });

    await expect(promise).rejects.toThrow();
  });

  it('should handle deleteDocument error', async () => {
    const promise = service.deleteDocument('assistant1', 'doc-123');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/doc-123').flush({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
    });

    await expect(promise).rejects.toThrow();
  });

  it('should handle getDocument error', async () => {
    const promise = service.getDocument('assistant1', 'doc-123');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents/doc-123').flush({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
    });

    await expect(promise).rejects.toThrow();
  });

  it('should handle listDocuments error', async () => {
    const promise = service.listDocuments('assistant1');
    
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/assistants/assistant1/documents').flush({ detail: 'Unauthorized' }, { status: 401, statusText: 'Unauthorized' });
    });

    await expect(promise).rejects.toThrow();
  });

  describe('handleApiError', () => {
    it('should handle HttpErrorResponse', () => {
      const httpError = new HttpErrorResponse({
        status: 404,
        statusText: 'Not Found',
        error: { detail: 'Document not found' }
      });
      
      const result = (service as any).handleApiError(httpError, 'Default message');
      
      expect(result).toBeInstanceOf(DocumentUploadError);
      expect(result.message).toBe('Document not found');
      expect(result.code).toBe('HTTP_404');
      expect(result.details.status).toBe(404);
    });

    it('should handle generic Error', () => {
      const error = new Error('Network error');
      
      const result = (service as any).handleApiError(error, 'Default message');
      
      expect(result).toBeInstanceOf(DocumentUploadError);
      expect(result.message).toBe('Network error');
      expect(result.code).toBe('UNKNOWN_ERROR');
    });

    it('should handle non-Error objects', () => {
      const error = 'String error';
      
      const result = (service as any).handleApiError(error, 'Default message');
      
      expect(result).toBeInstanceOf(DocumentUploadError);
      expect(result.message).toBe('Default message');
      expect(result.code).toBe('UNKNOWN_ERROR');
      expect(result.details.originalError).toBe('String error');
    });
  });
});