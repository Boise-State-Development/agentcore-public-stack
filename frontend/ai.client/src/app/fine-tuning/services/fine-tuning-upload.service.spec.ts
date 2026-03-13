import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { FineTuningUploadService } from './fine-tuning-upload.service';

describe('FineTuningUploadService', () => {
  let service: FineTuningUploadService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [FineTuningUploadService],
    });
    service = TestBed.inject(FineTuningUploadService);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  function createMockXHR(opts: { status?: number; responseText?: string; triggerError?: boolean } = {}) {
    const { status = 200, responseText = '', triggerError = false } = opts;

    // Use a class-based mock so `new XMLHttpRequest()` works properly
    const instances: MockXHRInstance[] = [];

    class MockXHRInstance {
      open = vi.fn();
      send = vi.fn();
      setRequestHeader = vi.fn();
      status = status;
      statusText = status === 200 ? 'OK' : 'Bad Request';
      responseText = responseText;
      upload: { onprogress: ((e: unknown) => void) | null } = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor() {
        instances.push(this);
        // eslint-disable-next-line @typescript-eslint/no-this-alias
        const self = this;

        this.send.mockImplementation(() => {
          if (triggerError) {
            setTimeout(() => self.onerror?.(), 0);
          } else {
            setTimeout(() => {
              self.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
              self.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 });
              self.onload?.();
            }, 0);
          }
        });
      }
    }

    vi.stubGlobal('XMLHttpRequest', MockXHRInstance as unknown as typeof XMLHttpRequest);

    return { getInstance: () => instances[instances.length - 1] };
  }

  it('should upload a file successfully with progress', async () => {
    const mock = createMockXHR({ status: 200 });
    const progressValues: number[] = [];
    const file = new File(['test'], 'test.jsonl', { type: 'application/json' });
    const url = 'https://s3.example.com/upload?content-type=application%2Fjson';

    await service.uploadFile(url, file, (progress) => progressValues.push(progress));

    const xhr = mock.getInstance();
    expect(xhr.open).toHaveBeenCalledWith('PUT', url);
    expect(xhr.setRequestHeader).toHaveBeenCalledWith('Content-Type', 'application/json');
    expect(xhr.send).toHaveBeenCalledWith(file);
    expect(progressValues).toContain(50);
    expect(progressValues).toContain(100);
  });

  it('should use content-type from presigned URL query params', async () => {
    const mock = createMockXHR({ status: 200 });
    const file = new File(['test'], 'test.txt', { type: 'text/plain' });
    const url = 'https://s3.example.com/upload?content-type=application%2Foctet-stream';

    await service.uploadFile(url, file, vi.fn());

    expect(mock.getInstance().setRequestHeader).toHaveBeenCalledWith('Content-Type', 'application/octet-stream');
  });

  it('should fall back to file.type when no content-type in URL', async () => {
    const mock = createMockXHR({ status: 200 });
    const file = new File(['test'], 'test.csv', { type: 'text/csv' });
    const url = 'https://s3.example.com/upload';

    await service.uploadFile(url, file, vi.fn());

    expect(mock.getInstance().setRequestHeader).toHaveBeenCalledWith('Content-Type', 'text/csv');
  });

  it('should reject on HTTP error status', async () => {
    createMockXHR({ status: 403, responseText: '' });
    const file = new File(['test'], 'test.jsonl', { type: 'application/json' });

    await expect(
      service.uploadFile('https://s3.example.com/upload', file, vi.fn()),
    ).rejects.toThrow('S3 upload failed: 403 Bad Request');
  });

  it('should reject on network error', async () => {
    createMockXHR({ triggerError: true });
    const file = new File(['test'], 'test.jsonl', { type: 'application/json' });

    await expect(
      service.uploadFile('https://s3.example.com/upload', file, vi.fn()),
    ).rejects.toThrow('Network error during file upload');
  });

  it('should parse S3 XML error responses', async () => {
    const xmlError = '<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>';
    createMockXHR({ status: 403, responseText: xmlError });
    const file = new File(['test'], 'test.jsonl', { type: 'application/json' });

    await expect(
      service.uploadFile('https://s3.example.com/upload', file, vi.fn()),
    ).rejects.toThrow('AccessDenied: Access Denied');
  });
});
