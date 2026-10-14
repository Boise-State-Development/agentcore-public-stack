import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import {
  FolderAttachment,
  FolderAttachmentError,
  FolderAttachmentService,
  folderSkipReasons,
  folderStatusLabel,
} from './folder-attachment.service';
import { ConfigService } from '../config.service';

function row(over: Partial<FolderAttachment> = {}, counts: Partial<FolderAttachment['counts']> = {}): FolderAttachment {
  return {
    folderAttachId: 'fa-1',
    sessionId: 's-1',
    connectorId: 'google-drive',
    connectorName: 'Google Drive',
    providerFolderId: 'p',
    folderName: 'Course',
    folderPath: null,
    status: 'ready',
    error: null,
    counts: { listed: 6, attached: 6, skippedType: 0, skippedSize: 0, failed: 0, truncated: false, ...counts },
    files: [],
    snapshotAt: null,
    createdAt: '',
    updatedAt: '',
    stale: false,
    ...over,
  };
}

describe('folder labels', () => {
  it.each([
    [row({ status: 'expanding' }, { listed: 0, attached: 0 }), 'Reading folder…'],
    [row({ status: 'expanding' }, { listed: 6, attached: 3 }), 'Adding 3 of 6…'],
    [row(), '6 files'],
    [row({}, { listed: 1, attached: 1 }), '1 file'],
    [row({ status: 'partial' }, { attached: 4, skippedType: 2 }), '4 of 6 files · 2 skipped'],
    [row({ status: 'partial' }, { attached: 100, listed: 100, truncated: true }), '100 files (limit reached)'],
    [row({ status: 'failed', error: 'needs_consent' }, { attached: 0, listed: 0 }), 'Reconnect to read this folder'],
    [row({ status: 'failed', error: 'listing_failed' }, { attached: 0, listed: 0 }), "Couldn't read this folder"],
  ])('%#: %s', (r, label) => {
    expect(folderStatusLabel(r)).toBe(label);
  });

  it('names each reason something was left out', () => {
    expect(folderSkipReasons(row({ error: 'quota_exceeded' }, { skippedType: 2, skippedSize: 1, failed: 1, truncated: true }))).toBe(
      '2 not a type the assistant can read (images, other formats); 1 too large; 1 could not be downloaded; the folder has more files than one attachment takes; your file storage is full',
    );
    expect(folderSkipReasons(row())).toBe('');
  });
});

describe('FolderAttachmentService', () => {
  let service: FolderAttachmentService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => '/api' } },
      ],
    });
    service = TestBed.inject(FolderAttachmentService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('attaches with the connector, folder and path', async () => {
    const pending = service.attach('s 1', { connectorId: 'google-drive', folderId: 'f1', folderName: 'Course', folderPath: 'My Drive/Course' });
    const req = http.expectOne('/api/sessions/s%201/folders');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ connectorId: 'google-drive', folderId: 'f1', folderName: 'Course', folderPath: 'My Drive/Course' });
    req.flush(row({ status: 'expanding' }));
    expect((await pending).status).toBe('expanding');
  });

  it('reads, resumes and removes by id', async () => {
    const got = service.get('s-1', 'fa-1');
    http.expectOne('/api/sessions/s-1/folders/fa-1').flush(row());
    await got;
    const resumed = service.resume('s-1', 'fa-1');
    const r = http.expectOne('/api/sessions/s-1/folders/fa-1/resume');
    expect(r.request.method).toBe('POST');
    r.flush(row({ status: 'expanding' }));
    await resumed;
    const removed = service.remove('s-1', 'fa-1');
    const del = http.expectOne('/api/sessions/s-1/folders/fa-1');
    expect(del.request.method).toBe('DELETE');
    del.flush(null, { status: 204, statusText: 'No Content' });
    await removed;
  });

  it('treats a row already gone as removed', async () => {
    const removed = service.remove('s-1', 'gone');
    http.expectOne('/api/sessions/s-1/folders/gone').flush({ detail: 'Folder attachment not found' }, { status: 404, statusText: 'Not Found' });
    await expect(removed).resolves.toBeUndefined();
  });

  it('carries the status and the server detail on a failure', async () => {
    const got = service.get('s-1', 'fa-1');
    http.expectOne('/api/sessions/s-1/folders/fa-1').flush({ detail: 'Folder attachment not found' }, { status: 404, statusText: 'Not Found' });
    const error = await got.catch((e) => e);
    expect(error).toBeInstanceOf(FolderAttachmentError);
    expect(error.status).toBe(404);
    expect(error.message).toBe('Folder attachment not found');
  });

  it('turns a needs-consent 409 into a connect message', async () => {
    const attached = service.attach('s-1', { connectorId: 'g', folderId: 'f', folderName: 'n' });
    http.expectOne('/api/sessions/s-1/folders').flush({}, { status: 409, statusText: 'Conflict' });
    const error = await attached.catch((e) => e);
    expect(error.status).toBe(409);
    expect(error.message).toBe('Connect Google Drive to attach this folder.');
  });
});
