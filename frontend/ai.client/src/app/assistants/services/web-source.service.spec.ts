import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { WebSourceService, WebSourceError } from './web-source.service';
import { ConfigService } from '../../services/config.service';
import { CrawlJob } from '../models/web-source.model';

const BASE = 'http://localhost:8000/assistants/assistant1/web-sources';

function stubCrawl(overrides: Partial<CrawlJob> = {}): CrawlJob {
  return {
    crawlId: 'CRAWL-abc123',
    assistantId: 'assistant1',
    rootUrl: 'https://example.com/docs/',
    status: 'complete',
    settings: { maxDepth: 1, maxPages: 25 },
    discoveredCount: 12,
    fetchedCount: 10,
    failedCount: 2,
    startedAt: '2026-07-14T00:00:00Z',
    startedByUserId: 'user-1',
    ...overrides,
  } as CrawlJob;
}

describe('WebSourceService', () => {
  let service: WebSourceService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        WebSourceService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(WebSourceService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should list crawls', async () => {
    const crawls = [stubCrawl()];

    const promise = service.listCrawls('assistant1');

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/crawls`);
      expect(req.request.method).toBe('GET');
      req.flush({ crawls });
    });

    expect(await promise).toEqual(crawls);
  });

  it('should delete a crawl', async () => {
    const promise = service.deleteCrawl('assistant1', 'CRAWL-abc123');

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/crawls/CRAWL-abc123`);
      expect(req.request.method).toBe('DELETE');
      req.flush(null, { status: 204, statusText: 'No Content' });
    });

    await expect(promise).resolves.toBeUndefined();
  });

  it('should surface the server message when a running crawl refuses deletion', async () => {
    const promise = service.deleteCrawl('assistant1', 'CRAWL-abc123');
    const caught = promise.catch((err: unknown) => err);

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/crawls/CRAWL-abc123`);
      req.flush(
        { detail: 'This crawl is still running. Wait for it to finish, then remove it.' },
        { status: 409, statusText: 'Conflict' },
      );
    });

    const error = (await caught) as WebSourceError;
    expect(error).toBeInstanceOf(WebSourceError);
    expect(error.status).toBe(409);
    expect(error.code).toBe('HTTP_409');
    expect(error.message).toContain('still running');
  });
});
