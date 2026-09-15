import { TestBed } from '@angular/core/testing';
import {
  provideHttpClient,
  withInterceptorsFromDi,
} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  FilePreviewError,
  FilePreviewHttpService,
} from './file-preview-http.service';
import { DOCX_MIME } from './file-preview.model';
import { ConfigService } from '../../../services/config.service';

describe('FilePreviewHttpService', () => {
  let service: FilePreviewHttpService;
  let httpMock: HttpTestingController;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
      ],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    service = TestBed.inject(FilePreviewHttpService);
    httpMock = TestBed.inject(HttpTestingController);

    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** Answer the preview-url leg with the given metadata. */
  function flushPreviewUrl(overrides: Record<string, unknown> = {}): void {
    const req = httpMock.expectOne('/api/files/up1/preview-url');
    expect(req.request.method).toBe('GET');
    req.flush({
      uploadId: 'up1',
      url: 'https://bucket.s3.us-west-2.amazonaws.com/key?X-Amz-Signature=abc',
      expiresAt: '2026-01-01T00:00:00Z',
      mimeType: DOCX_MIME,
      filename: 'plan.docx',
      ...overrides,
    });
  }

  it('resolves the presigned URL and returns the fetched bytes', async () => {
    const bytes = new ArrayBuffer(8);
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(bytes),
    });

    const pending = service.fetchDocument('up1');
    flushPreviewUrl();
    const doc = await pending;

    expect(doc.bytes).toBe(bytes);
    expect(doc.filename).toBe('plan.docx');
    expect(doc.mimeType).toBe(DOCX_MIME);
  });

  it('fetches S3 without credentials', async () => {
    // S3 answers a CORS GET without Access-Control-Allow-Credentials, so
    // a credentialed request is rejected by the browser before it is
    // sent. The signature in the URL is the authorization.
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
    });

    const pending = service.fetchDocument('up1');
    flushPreviewUrl();
    await pending;

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('X-Amz-Signature'),
      { credentials: 'omit' },
    );
  });

  it('refuses a file the server reports as a different type', async () => {
    const pending = service.fetchDocument('up1');
    flushPreviewUrl({ mimeType: 'application/pdf' });

    await expect(pending).rejects.toThrow(FilePreviewError);
    await expect(pending).rejects.toMatchObject({ retryable: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('reports a missing file as unavailable', async () => {
    const pending = service.fetchDocument('up1');
    httpMock
      .expectOne('/api/files/up1/preview-url')
      .flush('nope', { status: 404, statusText: 'Not Found' });

    await expect(pending).rejects.toMatchObject({
      message: 'This document is no longer available.',
    });
  });

  it('marks a failed S3 download as retryable', async () => {
    // A presigned URL that expired between minting and fetching is the
    // common case here, and re-minting genuinely fixes it.
    fetchMock.mockResolvedValue({ ok: false, status: 403 });

    const pending = service.fetchDocument('up1');
    flushPreviewUrl();

    await expect(pending).rejects.toMatchObject({ retryable: true });
  });

  it('marks a network failure as retryable', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    const pending = service.fetchDocument('up1');
    flushPreviewUrl();

    await expect(pending).rejects.toMatchObject({ retryable: true });
  });
});
