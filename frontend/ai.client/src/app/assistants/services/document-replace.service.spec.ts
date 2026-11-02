import { describe, it, expect, beforeEach, afterEach, vi, type Mock } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import {
  DocumentReplaceService,
  DocumentReplaceTransport,
  fileExtension,
  hasSameExtension,
  isByteCapError,
  orderReplacementsAdjacent,
} from './document-replace.service';
import { DocumentUploadError } from './document.service';
import { Document } from '../models/document.model';
import { ToastService } from '../../services/toast/toast.service';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';

function doc(overrides: Partial<Document> = {}): Document {
  return {
    documentId: 'old',
    assistantId: 'ast-1',
    filename: 'notes.pdf',
    contentType: 'application/pdf',
    sizeBytes: 10,
    status: 'complete',
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    ...overrides,
  } as Document;
}

type AnyFn = (...args: any[]) => any;

function transport(): Record<keyof DocumentReplaceTransport, Mock<AnyFn>> {
  const fn = () => vi.fn<AnyFn>();
  return {
    requestUploadUrl: fn().mockResolvedValue({ documentId: 'new', uploadUrl: 'https://s3/put' }),
    uploadToS3: fn().mockResolvedValue(undefined),
    reportUploadFailure: fn().mockResolvedValue(undefined),
    refresh: fn().mockResolvedValue(undefined),
    waitForTerminal: fn().mockResolvedValue(doc({ documentId: 'new', status: 'complete' })),
    deleteDocument: fn().mockResolvedValue(undefined),
  };
}

function unloadPrevented(): boolean {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

describe('document replace helpers', () => {
  it('compares extensions without regard to case', () => {
    expect(fileExtension('Report.PDF')).toBe('.pdf');
    expect(fileExtension('README')).toBe('');
    expect(hasSameExtension('Report.PDF', 'x.pdf')).toBe(true);
    expect(hasSameExtension('a.docx', 'a.pdf')).toBe(false);
    expect(hasSameExtension('README', 'LICENSE')).toBe(true);
  });

  it('puts a replacement right after its original, whatever the server order', () => {
    const docs = [{ documentId: 'new' }, { documentId: 'x' }, { documentId: 'old' }, { documentId: 'y' }];
    const ordered = orderReplacementsAdjacent(docs, new Map([['new', 'old']]));
    expect(ordered.map((d) => d.documentId)).toEqual(['x', 'old', 'new', 'y']);
  });

  it('leaves a replacement in place when its original is gone', () => {
    const docs = [{ documentId: 'new' }, { documentId: 'x' }];
    const ordered = orderReplacementsAdjacent(docs, new Map([['new', 'old']]));
    expect(ordered.map((d) => d.documentId)).toEqual(['new', 'x']);
  });

  it('recognises the byte cap in both error shapes', () => {
    expect(isByteCapError(new DocumentUploadError('over', 'HTTP_413'))).toBe(true);
    expect(isByteCapError(new HttpErrorResponse({ status: 413 }))).toBe(true);
    expect(isByteCapError(new DocumentUploadError('nope', 'HTTP_500'))).toBe(false);
    expect(isByteCapError(new Error('x'))).toBe(false);
  });
});

describe('DocumentReplaceService', () => {
  let service: DocumentReplaceService;
  let tasks: BackgroundTaskService;
  const dialog = { open: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
  const picked = new File(['v2'], 'Notes v2.PDF', { type: 'application/pdf' });

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    TestBed.configureTestingModule({
      providers: [
        { provide: Dialog, useValue: dialog },
        { provide: ToastService, useValue: toast },
      ],
    });
    service = TestBed.inject(DocumentReplaceService);
    tasks = TestBed.inject(BackgroundTaskService);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('uploads under the original name, waits for it, then deletes the original', async () => {
    const t = transport();
    const outcome = await service.replace(doc(), picked, t);

    expect(outcome.kind).toBe('replaced');
    const sent = t.requestUploadUrl.mock.calls[0][0] as File;
    expect(sent.name).toBe('notes.pdf');
    expect(sent.type).toBe('application/pdf');
    expect(t.deleteDocument).toHaveBeenCalledWith('old');
    const order = [t.requestUploadUrl, t.uploadToS3, t.waitForTerminal, t.deleteDocument].map(
      (f) => f.mock.invocationCallOrder[0],
    );
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(toast.success).toHaveBeenCalled();
    expect(service.replacementFor('new')).toBeNull();
    expect(service.inFlight()).toBe(false);
  });

  it('keeps the original when the new version fails to process', async () => {
    const t = transport();
    t.waitForTerminal.mockResolvedValue(doc({ documentId: 'new', status: 'failed', errorMessage: 'Parse error' }));
    const outcome = await service.replace(doc(), picked, t);

    expect(outcome.kind).toBe('failed');
    expect(t.deleteDocument).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(service.replacementFor('new')).toBe('old');
  });

  it('reports a failed S3 upload and keeps the original', async () => {
    const t = transport();
    t.uploadToS3.mockRejectedValue(new DocumentUploadError('S3 down', 'S3_UPLOAD_FAILED', { status: 500 }));
    const outcome = await service.replace(doc(), picked, t);

    expect(outcome.kind).toBe('failed');
    expect(t.reportUploadFailure).toHaveBeenCalledWith('new', 'S3 down', expect.any(String));
    expect(t.refresh).toHaveBeenCalled();
    expect(t.deleteDocument).not.toHaveBeenCalled();
    expect(service.replacementFor('new')).toBe('old');
  });

  it('asks before removing the original when the byte cap has no room, and stops on no', async () => {
    const t = transport();
    t.requestUploadUrl.mockRejectedValue(new DocumentUploadError('over cap', 'HTTP_413'));
    dialog.open.mockReturnValue({ closed: of(false) });
    const outcome = await service.replace(doc(), picked, t);

    expect(outcome.kind).toBe('cancelled');
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(dialog.open.mock.calls[0][1].data.destructive).toBe(true);
    expect(t.deleteDocument).not.toHaveBeenCalled();
    expect(t.uploadToS3).not.toHaveBeenCalled();
  });

  it('removes the original first when the user confirms the byte-cap fallback', async () => {
    const t = transport();
    t.requestUploadUrl
      .mockRejectedValueOnce(new DocumentUploadError('over cap', 'HTTP_413'))
      .mockResolvedValueOnce({ documentId: 'new', uploadUrl: 'https://s3/put' });
    dialog.open.mockReturnValue({ closed: of(true) });
    const outcome = await service.replace(doc(), picked, t);

    expect(outcome.kind).toBe('replaced');
    expect(t.deleteDocument).toHaveBeenCalledTimes(1);
    expect(t.deleteDocument.mock.invocationCallOrder[0]).toBeLessThan(
      t.requestUploadUrl.mock.invocationCallOrder[1],
    );
    expect(t.uploadToS3).toHaveBeenCalled();
  });

  it('rejects a different file type or an oversized file without touching the server', async () => {
    const t = transport();
    expect((await service.replace(doc(), new File(['x'], 'notes.docx'), t)).kind).toBe('rejected');
    const big = new File(['x'], 'notes.pdf');
    Object.defineProperty(big, 'size', { value: 11 * 1024 * 1024 });
    expect((await service.replace(doc(), big, t)).kind).toBe('rejected');
    expect(toast.error).toHaveBeenCalledTimes(2);
    for (const fn of Object.values(t)) expect(fn).not.toHaveBeenCalled();
    expect(tasks.tasks()).toHaveLength(0);
  });

  it('warns the user to keep the tab open while it runs, and clears the warning after', async () => {
    const t = transport();
    let finishUpload!: () => void;
    t.uploadToS3.mockReturnValue(new Promise<void>((resolve) => (finishUpload = resolve)));
    const running = service.replace(doc(), picked, t);
    await vi.waitFor(() => expect(t.uploadToS3).toHaveBeenCalled());

    expect(tasks.tasks()).toHaveLength(1);
    expect(service.inFlight()).toBe(true);
    expect(service.isBusy('old')).toBe(true);
    expect(service.isBusy('new')).toBe(true);
    expect(unloadPrevented()).toBe(true);
    expect((await service.replace(doc({ documentId: 'other' }), picked, transport())).kind).toBe('blocked');

    finishUpload();
    await running;
    expect(tasks.tasks()).toHaveLength(0);
    expect(service.inFlight()).toBe(false);
    expect(unloadPrevented()).toBe(false);
  });

  it('keeps both versions when the wait ends without an answer', async () => {
    const t = transport();
    t.waitForTerminal.mockResolvedValue(null);
    const outcome = await service.replace(doc(), picked, t);
    expect(outcome.kind).toBe('interrupted');
    expect(t.deleteDocument).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalled();
  });
});
