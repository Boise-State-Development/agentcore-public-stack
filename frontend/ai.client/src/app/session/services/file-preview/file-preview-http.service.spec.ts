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
import { DOCX_MIME, PPTX_MIME } from './file-preview.model';
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
    expect(doc.kind).toBe('docx');
  });

  it('resolves a .pptx to the pptx viewer', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    });

    const pending = service.fetchDocument('up1');
    flushPreviewUrl({ mimeType: PPTX_MIME, filename: 'deck.pptx' });
    const doc = await pending;

    expect(doc.kind).toBe('pptx');
    expect(doc.mimeType).toBe(PPTX_MIME);
  });

  it('accepts a .csv the browser mislabelled as an Excel type', async () => {
    // Windows reports application/vnd.ms-excel for a .csv whenever Excel
    // is the registered handler, and the recorded MIME is whatever the
    // browser said at upload time. Refusing it would fail the preview on
    // the most ordinary desktop in the building.
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(4)),
    });

    const pending = service.fetchDocument('up1');
    flushPreviewUrl({
      mimeType: 'application/vnd.ms-excel',
      filename: 'export.csv',
    });
    const doc = await pending;

    expect(doc.kind).toBe('csv');
  });

  it('still refuses an .xls, which shares that MIME type', async () => {
    // The extension is what chooses the viewer, so widening the accepted
    // MIME list for .csv must not make the legacy binary format
    // previewable.
    const pending = service.fetchDocument('up1');
    flushPreviewUrl({
      mimeType: 'application/vnd.ms-excel',
      filename: 'budget.xls',
    });

    await expect(pending).rejects.toThrow(FilePreviewError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a file whose MIME type contradicts its extension', async () => {
    // The extension picked the viewer before any request was made, so a
    // file named .pptx that the server knows to be a .docx has to fail
    // here rather than reach a renderer that cannot read it.
    const pending = service.fetchDocument('up1');
    flushPreviewUrl({ mimeType: DOCX_MIME, filename: 'deck.pptx' });

    await expect(pending).rejects.toThrow(FilePreviewError);
    await expect(pending).rejects.toMatchObject({ retryable: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a format the pane cannot preview at all', async () => {
    const pending = service.fetchDocument('up1');
    flushPreviewUrl({ mimeType: 'application/pdf', filename: 'report.pdf' });

    await expect(pending).rejects.toThrow(FilePreviewError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses to fetch the bytes of a kind that is read server-side', async () => {
    // An .xlsx has a viewer, but not one that reads bytes. Letting it
    // through here would pull a whole workbook into the browser and
    // then throw it away, so the byte path rejects it before the fetch.
    const pending = service.fetchDocument('up1');
    flushPreviewUrl({
      mimeType:
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      filename: 'budget.xlsx',
    });

    await expect(pending).rejects.toThrow(FilePreviewError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  describe('fetchSheets', () => {
    it('asks app-api for rows and never touches S3', async () => {
      // The .xlsx path has one leg, not two: no presigned URL, and the
      // workbook never reaches the browser.
      const pending = service.fetchSheets('up1');
      const req = httpMock.expectOne(
        '/api/files/up1/sheet-preview',
      );
      expect(req.request.method).toBe('GET');
      req.flush({
        uploadId: 'up1',
        filename: 'budget.xlsx',
        sheets: [],
        truncated: false,
      });

      await expect(pending).resolves.toMatchObject({ filename: 'budget.xlsx' });
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it.each([
      [413, false, 'too large'],
      [422, false, 'corrupt or password-protected'],
      [415, false, "isn't a spreadsheet"],
      [404, true, 'no longer available'],
      [500, true, 'Check your connection'],
    ])(
      'maps %i to a %s-retryable message',
      async (status, retryable, fragment) => {
        // The route distinguishes these on purpose: 413 and 422 are
        // permanent facts about the file, while a 5xx is worth another go.
        const pending = service.fetchSheets('up1');
        httpMock
          .expectOne('/api/files/up1/sheet-preview')
          .flush('err', { status, statusText: 'error' });

        await expect(pending).rejects.toThrow(FilePreviewError);
        await expect(pending).rejects.toMatchObject({ retryable });
        await expect(pending).rejects.toThrow(
          expect.objectContaining({
            message: expect.stringContaining(fragment),
          }),
        );
      },
    );
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
