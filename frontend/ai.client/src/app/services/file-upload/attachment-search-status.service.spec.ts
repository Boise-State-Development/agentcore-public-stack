import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  AttachmentSearchStatusService,
  largeDocumentChip,
  SEARCH_STATUS_POLL_DELAYS_MS,
  SEARCH_STATUS_POLL_LIMIT_MS,
} from './attachment-search-status.service';
import { FileUploadService, type AttachmentSearchStatus } from './file-upload.service';

/**
 * A large document's chip follows its search index
 * (docs/specs/large-document-attachments.md §4G): one poll per upload, while the
 * status is still moving, bounded in time, and stopped by an error.
 */
describe('AttachmentSearchStatusService', () => {
  let getSearchStatus: ReturnType<typeof vi.fn>;
  let service: AttachmentSearchStatusService;

  function answers(...statuses: (AttachmentSearchStatus | Error)[]) {
    for (const status of statuses) {
      if (status instanceof Error) getSearchStatus.mockRejectedValueOnce(status);
      else getSearchStatus.mockResolvedValueOnce({ uploadId: 'up-1', status });
    }
  }

  beforeEach(() => {
    vi.useFakeTimers();
    getSearchStatus = vi.fn();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: FileUploadService, useValue: { getSearchStatus } }],
    });
    service = TestBed.inject(AttachmentSearchStatusService);
  });

  afterEach(() => vi.useRealTimers());

  it('polls while indexing and stops once searchable', async () => {
    answers('preparing', 'indexing', 'searchable');
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(0);
    expect(service.statusFor('up-1')).toBe('preparing');

    await vi.advanceTimersByTimeAsync(SEARCH_STATUS_POLL_DELAYS_MS[0]);
    expect(service.statusFor('up-1')).toBe('indexing');

    await vi.advanceTimersByTimeAsync(SEARCH_STATUS_POLL_DELAYS_MS[1]);
    expect(service.statusFor('up-1')).toBe('searchable');

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(getSearchStatus).toHaveBeenCalledTimes(3);
  });

  it('shares one poll between every card that shows the upload', async () => {
    answers('searchable');
    service.track('up-1');
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(0);
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(getSearchStatus).toHaveBeenCalledTimes(1);
  });

  it('settles at once when the deployment has no knowledge base', async () => {
    answers('off');
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(getSearchStatus).toHaveBeenCalledTimes(1);
    expect(largeDocumentChip(service.statusFor('up-1'))).toBe('read-on-demand');
  });

  it('stops on an error and can be tracked again later', async () => {
    answers(new Error('404'), 'searchable');
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(getSearchStatus).toHaveBeenCalledTimes(1);
    expect(service.statusFor('up-1')).toBeNull();

    service.track('up-1');
    await vi.advanceTimersByTimeAsync(0);
    expect(service.statusFor('up-1')).toBe('searchable');
  });

  it('gives up after the time limit', async () => {
    getSearchStatus.mockResolvedValue({ uploadId: 'up-1', status: 'indexing' });
    service.track('up-1');
    await vi.advanceTimersByTimeAsync(SEARCH_STATUS_POLL_LIMIT_MS + 60_000);
    const calls = getSearchStatus.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(getSearchStatus.mock.calls.length).toBe(calls);
    expect(calls).toBeLessThan(70);
  });
});

describe('largeDocumentChip', () => {
  it.each([
    [null, 'read-on-demand'],
    ['off', 'read-on-demand'],
    ['unavailable', 'read-on-demand'],
    ['preparing', 'indexing'],
    ['indexing', 'indexing'],
    ['evicted', 'indexing'],
    ['searchable', 'searchable'],
  ] as const)('%s → %s', (status, chip) => {
    expect(largeDocumentChip(status)).toBe(chip);
  });
});
