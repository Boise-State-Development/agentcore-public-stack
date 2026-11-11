import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ConfigService } from '../config.service';
import { FEATURES } from '../features';
import {
  AttachmentLimitsService,
  FALLBACK_ATTACHMENT_LIMITS,
  filesPerMessageOf,
  parseAttachmentLimits,
} from './attachment-limits';
import { attachmentSizeLimitFor } from './file-upload.service';

const MB = 1024 * 1024;

/** What app-api serves today with default settings (test_file_limits_route.py pins the same). */
const SERVED = {
  profile: 'legacy',
  filesPerMessage: 5,
  imagesPerMessage: null,
  pdfMaxPages: null,
  inlineDocumentMaxBytes: 4 * MB,
  inlineBudgetBytes: 7_000_000,
  maxBytes: { document: 4 * MB, image: 4 * MB, spreadsheet: 25 * MB, presentation: 25 * MB, largeDocument: 50 * MB },
  largeDocuments: true,
  folders: { enabled: true, perMessage: 2 },
};

describe('FALLBACK_ATTACHMENT_LIMITS', () => {
  it("is the backend's legacy profile with default settings", () => {
    expect(parseAttachmentLimits(SERVED)).toEqual(FALLBACK_ATTACHMENT_LIMITS);
  });
});

describe('parseAttachmentLimits', () => {
  it("takes the server's explicit null as no limit", () => {
    const parsed = parseAttachmentLimits({ ...SERVED, filesPerMessage: null, inlineBudgetBytes: null });
    expect(parsed.filesPerMessage).toBeNull();
    expect(parsed.inlineBudgetBytes).toBeNull();
    expect(filesPerMessageOf(parsed)).toBe(Number.POSITIVE_INFINITY);
  });

  it('keeps the fallback for a missing or malformed field', () => {
    const parsed = parseAttachmentLimits({ maxBytes: { document: 'big', spreadsheet: -1 } });
    expect(parsed).toEqual(FALLBACK_ATTACHMENT_LIMITS);
  });

  it('turns large documents off when the server has no ceiling for them', () => {
    const parsed = parseAttachmentLimits({ ...SERVED, largeDocuments: false, maxBytes: { ...SERVED.maxBytes, largeDocument: null } });
    expect(parsed.largeDocuments).toBe(false);
    expect(attachmentSizeLimitFor('a.pdf', 'application/pdf', parsed)).toBe(4 * MB);
  });
});

describe('AttachmentLimitsService', () => {
  let http: HttpTestingController;

  function setup(largeDocumentAttachments: boolean): AttachmentLimitsService {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://api') } },
        { provide: FEATURES, useValue: { largeDocumentAttachments, folderAttachments: true } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    return TestBed.inject(AttachmentLimitsService);
  }

  afterEach(() => http.verify());

  describe('before the fetch answers', () => {
    let service: AttachmentLimitsService;
    beforeEach(() => (service = setup(false)));

    it('judges by the fallback, with this build’s large-document flag', () => {
      expect(service.loaded()).toBe(false);
      expect(service.filesPerMessage()).toBe(5);
      expect(service.limits().largeDocuments).toBe(false);
      expect(attachmentSizeLimitFor('a.pdf', 'application/pdf', service.limits())).toBe(4 * MB);
    });
  });

  it('takes the served limits once GET /files/limits answers', async () => {
    const service = setup(false);
    const done = service.load();
    http.expectOne('http://api/files/limits').flush({ ...SERVED, filesPerMessage: 20, maxBytes: { ...SERVED.maxBytes, image: 20 * MB } });
    await done;
    expect(service.loaded()).toBe(true);
    expect(service.filesPerMessage()).toBe(20);
    expect(service.limits().largeDocuments).toBe(true);
    expect(attachmentSizeLimitFor('a.png', 'image/png', service.limits())).toBe(20 * MB);
  });

  it('fetches once however many components call load()', async () => {
    const service = setup(true);
    const first = service.load();
    const second = service.load();
    expect(first).toBe(second);
    http.expectOne('http://api/files/limits').flush(SERVED);
    await first;
    await service.load();
    http.expectNone('http://api/files/limits');
  });

  it('keeps the fallback when the fetch fails, and can retry', async () => {
    const service = setup(true);
    const failed = service.load();
    http.expectOne('http://api/files/limits').flush('no', { status: 503, statusText: 'Unavailable' });
    await failed;
    expect(service.loaded()).toBe(false);
    expect(service.limits().filesPerMessage).toBe(5);
    const retry = service.load();
    http.expectOne('http://api/files/limits').flush(SERVED);
    await retry;
    expect(service.loaded()).toBe(true);
  });
});
