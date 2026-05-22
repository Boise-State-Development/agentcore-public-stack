import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { FileSourceService, FileSourceError } from './file-source.service';
import { ConfigService } from '../../services/config.service';

const API = 'http://localhost:8000';

describe('FileSourceService', () => {
  let service: FileSourceService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        FileSourceService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(FileSourceService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('lists file sources from the catalog response', async () => {
    const promise = service.listFileSources();
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/file-sources`);
      // app-api needs the callback URL to resolve OAuth tokens — without it
      // the backend raises CallbackUrlUnavailableError (503).
      expect(req.request.headers.get('OAuth2CallbackUrl')).toMatch(/\/oauth-complete$/);
      req.flush({
        fileSources: [
          { providerId: 'google', displayName: 'Google Drive', iconName: 'heroCloud', connected: true },
        ],
      });
    });
    const result = await promise;
    expect(result).toHaveLength(1);
    expect(result[0].providerId).toBe('google');
    expect(result[0].connected).toBe(true);
  });

  it('sends the OAuth2CallbackUrl header on every token-resolving call', async () => {
    const calls = [
      { trigger: () => service.listRoots('google'), url: `${API}/connectors/google/roots` },
      { trigger: () => service.browse('google', 'f1'), url: `${API}/connectors/google/browse` },
      { trigger: () => service.search('google', 'q'), url: `${API}/connectors/google/search` },
    ];
    for (const call of calls) {
      const promise = call.trigger();
      await vi.waitFor(() => {
        const req = httpMock.expectOne((r) => r.url === call.url);
        expect(req.request.headers.get('OAuth2CallbackUrl')).toMatch(/\/oauth-complete$/);
        req.flush({ entries: [], breadcrumbs: [], roots: [] });
      });
      await promise;
    }

    const importPromise = service.importDocuments('AST-1', 'google', [{ fileId: 'f', name: 'n' }]);
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/assistants/AST-1/documents/import`);
      expect(req.request.headers.get('OAuth2CallbackUrl')).toMatch(/\/oauth-complete$/);
      req.flush({ documents: [] });
    });
    await importPromise;
  });

  it('lists roots for a connector', async () => {
    const promise = service.listRoots('google');
    await vi.waitFor(() => {
      httpMock
        .expectOne(`${API}/connectors/google/roots`)
        .flush({ roots: [{ id: 'root', name: 'My Drive' }] });
    });
    const result = await promise;
    expect(result).toEqual([{ id: 'root', name: 'My Drive' }]);
  });

  it('browse sends folder_id and cursor as query params', async () => {
    const promise = service.browse('google', 'folder-1', 'cursor-abc');
    await vi.waitFor(() => {
      const req = httpMock.expectOne(
        (r) => r.url === `${API}/connectors/google/browse`,
      );
      expect(req.request.params.get('folder_id')).toBe('folder-1');
      expect(req.request.params.get('cursor')).toBe('cursor-abc');
      req.flush({ entries: [], breadcrumbs: [], nextCursor: null });
    });
    await promise;
  });

  it('browse omits the cursor param when not provided', async () => {
    const promise = service.browse('google', 'folder-1');
    await vi.waitFor(() => {
      const req = httpMock.expectOne(
        (r) => r.url === `${API}/connectors/google/browse`,
      );
      expect(req.request.params.has('cursor')).toBe(false);
      req.flush({ entries: [], breadcrumbs: [] });
    });
    await promise;
  });

  it('search sends the query param', async () => {
    const promise = service.search('google', 'budget');
    await vi.waitFor(() => {
      const req = httpMock.expectOne((r) => r.url === `${API}/connectors/google/search`);
      expect(req.request.params.get('query')).toBe('budget');
      req.flush({ entries: [], breadcrumbs: [] });
    });
    await promise;
  });

  it('imports documents with connectorId and files in the body', async () => {
    const files = [{ fileId: 'f1', name: 'a.txt' }];
    const promise = service.importDocuments('AST-1', 'google', files);
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/assistants/AST-1/documents/import`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ connectorId: 'google', files });
      req.flush({ documents: [{ documentId: 'DOC-1' }] });
    });
    const result = await promise;
    expect(result.documents[0].documentId).toBe('DOC-1');
  });

  it('wraps an HTTP 409 into a FileSourceError carrying the status', async () => {
    const promise = service.listRoots('google');
    await vi.waitFor(() => {
      httpMock
        .expectOne(`${API}/connectors/google/roots`)
        .flush({ detail: 'not connected' }, { status: 409, statusText: 'Conflict' });
    });
    await expect(promise).rejects.toMatchObject({
      name: 'FileSourceError',
      code: 'HTTP_409',
      status: 409,
    });
    await expect(promise).rejects.toBeInstanceOf(FileSourceError);
  });
});
