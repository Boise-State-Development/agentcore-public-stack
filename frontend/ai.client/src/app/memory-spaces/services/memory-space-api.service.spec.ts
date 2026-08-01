import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { Observable } from 'rxjs';
import { MemorySpaceApiService } from './memory-space-api.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../../services/config.service';

const BASE = 'http://localhost:8000/memory/spaces';

describe('MemorySpaceApiService', () => {
  let service: MemorySpaceApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        MemorySpaceApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(MemorySpaceApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  /**
   * Regression guard for the kill switch. While `MEMORY_SPACES_ENABLED` is off the
   * backend 404s this whole surface on purpose, and MemorySpaceService reads that as
   * "feature unavailable" and hides the nav entry. error.interceptor toasts every
   * non-401 unless the request opts out, so without SUPPRESS_ERROR_TOAST the feature
   * hides itself and then pops a dialog naming an endpoint the user should not see.
   *
   * Asserted per call rather than once on `list()`: a dark-stopped environment 404s
   * every one of these, and a deep link to a space detail page hits `get`/`listEntries`
   * without ever calling `list`.
   */
  // Widened to Observable<unknown> so the thunks form one callable signature —
  // a heterogeneous union of Observable<T> has no compatible `.subscribe`.
  const calls: Array<[string, () => Observable<unknown>]> = [
    ['list', () => service.list()],
    ['create', () => service.create({ name: 'x', template: 'blank' })],
    ['get', () => service.get('spc_1')],
    ['remove', () => service.remove('spc_1')],
    ['export', () => service.export('spc_1')],
    ['updateIndex', () => service.updateIndex('spc_1', '# hi')],
    ['readEntry', () => service.readEntry('spc_1', 'note')],
    ['upsertEntry', () => service.upsertEntry('spc_1', 'note', { body: 'x' })],
    ['deleteEntry', () => service.deleteEntry('spc_1', 'note')],
    ['listEntries', () => service.listEntries('spc_1')],
    ['listShares', () => service.listShares('spc_1')],
    ['addShare', () => service.addShare('spc_1', { email: 'a@b.c', permission: 'viewer' })],
    ['updateShare', () => service.updateShare('spc_1', 'a@b.c', 'editor')],
    ['removeShare', () => service.removeShare('spc_1', 'a@b.c')],
  ];

  it.each(calls)('%s opts out of the global error toast', (_name, call) => {
    call().subscribe({ next: () => undefined, error: () => undefined });

    const req = httpMock.expectOne(r => r.url.startsWith(BASE));
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush(null);
  });

  it('still sends the type filter on listEntries', () => {
    service.listEntries('spc_1', 'fact').subscribe();

    const req = httpMock.expectOne(r => r.url === `${BASE}/spc_1/entries`);
    expect(req.request.params.get('type')).toBe('fact');
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush({ entries: [] });
  });

  it('still requests a blob for export', () => {
    service.export('spc_1').subscribe();

    const req = httpMock.expectOne(`${BASE}/spc_1/export`);
    expect(req.request.responseType).toBe('blob');
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush(new Blob());
  });
});
