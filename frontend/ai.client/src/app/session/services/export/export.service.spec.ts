import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ExportService, ExportError } from './export.service';
import { ConfigService } from '../../../services/config.service';

const API = 'http://localhost:8000';

describe('ExportService', () => {
  let service: ExportService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ExportService,
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(ExportService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('lists export targets from the catalog response', async () => {
    const promise = service.listExportTargets();
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/export-targets`);
      // app-api needs the callback URL to resolve OAuth tokens — without it
      // the backend raises CallbackUrlUnavailableError (503).
      expect(req.request.headers.get('OAuth2CallbackUrl')).toMatch(/\/oauth-complete$/);
      req.flush({
        exportTargets: [
          {
            providerId: 'gdrive',
            displayName: 'Google Drive',
            iconName: 'heroCloud',
            connected: true,
            supportedFormats: ['google_doc', 'markdown'],
          },
        ],
      });
    });
    const result = await promise;
    expect(result).toHaveLength(1);
    expect(result[0].providerId).toBe('gdrive');
    expect(result[0].supportedFormats).toEqual(['google_doc', 'markdown']);
  });

  it('posts the export request and returns the result', async () => {
    const promise = service.exportSession('sess-1', {
      connectorId: 'gdrive',
      format: 'google_doc',
      include: {
        userMessages: true,
        assistantMessages: true,
        toolCalls: true,
        images: true,
        citations: true,
        reasoning: false,
        timestamps: false,
      },
    });
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/sessions/sess-1/export`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.connectorId).toBe('gdrive');
      expect(req.request.body.format).toBe('google_doc');
      // Token-resolving call must carry the callback header.
      expect(req.request.headers.get('OAuth2CallbackUrl')).toMatch(/\/oauth-complete$/);
      req.flush({
        fileId: 'file-123',
        name: 'My Chat',
        webViewLink: 'https://drive.example/file-123',
        receipt: {
          connectorId: 'gdrive',
          adapterKey: 'google-drive',
          format: 'google_doc',
          fileId: 'file-123',
          fileName: 'My Chat',
          webViewLink: 'https://drive.example/file-123',
          exportedAt: '2026-06-26T00:00:00Z',
        },
      });
    });
    const result = await promise;
    expect(result.fileId).toBe('file-123');
    expect(result.receipt.adapterKey).toBe('google-drive');
  });

  it('encodes the session id in the export URL', async () => {
    const promise = service.exportSession('a/b c', { connectorId: 'gdrive' });
    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${API}/sessions/a%2Fb%20c/export`);
      req.flush({ fileId: 'f', name: 'n', receipt: {} });
    });
    await promise;
  });

  it('wraps an HTTP 409 into an ExportError carrying the status', async () => {
    const promise = service.exportSession('sess-1', { connectorId: 'gdrive' });
    await vi.waitFor(() => {
      httpMock
        .expectOne(`${API}/sessions/sess-1/export`)
        .flush({ detail: 'not connected' }, { status: 409, statusText: 'Conflict' });
    });
    await expect(promise).rejects.toMatchObject({
      name: 'ExportError',
      code: 'HTTP_409',
      status: 409,
    });
    await expect(promise).rejects.toBeInstanceOf(ExportError);
  });
});
