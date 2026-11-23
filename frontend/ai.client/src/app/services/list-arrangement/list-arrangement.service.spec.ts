import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { environment } from '../../../environments/environment';
import { FEATURES } from '../features';
import { ConfigService } from '../config.service';
import { ListArrangementService } from './list-arrangement.service';
import { MAX_ARRANGEMENT_IDS, arrangeList, withHidden } from './list-arrangement.models';

const URL = '/api/users/me/arrangements/agents';

function setUp(organizeLists: boolean) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: FEATURES, useValue: { ...environment.features, organizeLists } },
      { provide: ConfigService, useValue: { appApiUrl: () => '/api' } },
    ],
  });
  return { service: TestBed.inject(ListArrangementService), http: TestBed.inject(HttpTestingController) };
}

describe('ListArrangementService', () => {
  let http: HttpTestingController;
  let service: ListArrangementService;

  beforeEach(() => {
    ({ service, http } = setUp(true));
  });

  afterEach(() => http.verify());

  /** Let the resource issue its GET, answer it, and let the value land. */
  async function answerGet(body: object, init?: { status: number; statusText: string }): Promise<void> {
    TestBed.tick();
    await Promise.resolve();
    http.expectOne((r) => r.method === 'GET' && r.url === URL).flush(body, init);
    await new Promise((resolve) => setTimeout(resolve));
  }

  it('fetches nothing until a page opens it', () => {
    TestBed.tick();
    http.expectNone(URL);
    expect(service.hiddenIds('agents')().size).toBe(0);
  });

  it('loads on open and refreshes on a later open', async () => {
    service.open('agents');
    await answerGet({ sort: 'recent', order: [], hidden: ['ast-1'] });
    expect([...service.hiddenIds('agents')()]).toEqual(['ast-1']);

    service.open('agents');
    expect(service.hiddenIds('agents')().size).toBe(1);
    await answerGet({ sort: 'recent', order: [], hidden: ['ast-1', 'ast-2'] });
    expect(service.hiddenIds('agents')().size).toBe(2);
  });

  it('reads a failed load as the defaults, so the page lists everything', async () => {
    service.open('agents');
    await answerGet({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
    expect(service.arrangement('agents')().sort).toBe('recent');
    expect(service.hiddenIds('agents')().size).toBe(0);
  });

  it('shows a hide at once and sends the whole arrangement, keeping ids the page cannot see', async () => {
    service.open('agents');
    await answerGet({ sort: 'recent', order: ['ast-gone'], hidden: ['ast-gone'] });

    const done = service.setHidden('agents', 'ast-1', true, new Set(['ast-1']));
    expect(service.hiddenIds('agents')().has('ast-1')).toBe(true);

    await Promise.resolve();
    const put = http.expectOne((r) => r.method === 'PUT' && r.url === URL);
    expect(put.request.body).toEqual({ sort: 'recent', order: ['ast-gone'], hidden: ['ast-gone', 'ast-1'] });
    put.flush({ ...put.request.body, updatedAt: 'now' });
    await done;
  });

  it('sends queued writes one after another, in order', async () => {
    const first = service.setHidden('agents', 'ast-1', true, new Set());
    const second = service.setHidden('agents', 'ast-2', true, new Set());
    await Promise.resolve();

    const one = http.expectOne((r) => r.method === 'PUT');
    expect(one.request.body.hidden).toEqual(['ast-1']);
    one.flush({});
    await first;
    await Promise.resolve();

    const two = http.expectOne((r) => r.method === 'PUT');
    expect(two.request.body.hidden).toEqual(['ast-1', 'ast-2']);
    two.flush({});
    await second;
  });

  it('re-reads the stored arrangement and rethrows when a write fails', async () => {
    service.open('agents');
    await answerGet({ sort: 'recent', order: [], hidden: [] });

    const write = service.setHidden('agents', 'ast-1', true, new Set(['ast-1']));
    await Promise.resolve();
    http.expectOne((r) => r.method === 'PUT').flush({}, { status: 503, statusText: 'Unavailable' });
    await expect(write).rejects.toBeTruthy();

    await answerGet({ sort: 'recent', order: [], hidden: [] });
    expect(service.hiddenIds('agents')().has('ast-1')).toBe(false);
  });

  it('does nothing at all while the flag is off', async () => {
    ({ service, http } = setUp(false));
    service.open('agents');
    await service.setHidden('agents', 'ast-1', true, new Set());
    TestBed.tick();
    http.expectNone(() => true);
    expect(service.hiddenIds('agents')().size).toBe(0);
  });
});

describe('arrangeList', () => {
  it('splits by hidden id and keeps the page order in both halves', () => {
    const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }];
    const { visible, hidden } = arrangeList(items, (i) => i.id, new Set(['c', 'a', 'gone']));
    expect(visible.map((i) => i.id)).toEqual(['b', 'd']);
    expect(hidden.map((i) => i.id)).toEqual(['a', 'c']);
  });
});

describe('withHidden', () => {
  it('adds once and removes cleanly', () => {
    expect(withHidden(['a'], 'a', true, new Set())).toEqual(['a']);
    expect(withHidden(['a', 'b'], 'a', false, new Set())).toEqual(['b']);
  });

  it('over the cap drops the oldest ids the page cannot see, never one it shows', () => {
    const full = Array.from({ length: MAX_ARRANGEMENT_IDS }, (_, i) => `id-${i}`);
    const onPage = new Set(['id-0', 'new']);
    const next = withHidden(full, 'new', true, onPage);
    expect(next).toHaveLength(MAX_ARRANGEMENT_IDS);
    expect(next).toContain('id-0');
    expect(next).toContain('new');
    expect(next).not.toContain('id-1');
  });
});
