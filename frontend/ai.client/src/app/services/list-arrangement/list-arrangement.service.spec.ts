import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { environment } from '../../../environments/environment';
import { FEATURES } from '../features';
import { ConfigService } from '../config.service';
import { ListArrangementService } from './list-arrangement.service';
import { DEFAULT_ARRANGEMENT, MAX_ARRANGEMENT_IDS, arrangeList, foldNew, withArranged, withHidden } from './list-arrangement.models';

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

    const done = service.setHidden('agents', 'ast-1', true, ['ast-1']);
    expect(service.hiddenIds('agents')().has('ast-1')).toBe(true);

    await Promise.resolve();
    const put = http.expectOne((r) => r.method === 'PUT' && r.url === URL);
    expect(put.request.body).toEqual({ sort: 'recent', order: ['ast-gone'], hidden: ['ast-gone', 'ast-1'] });
    put.flush({ ...put.request.body, updatedAt: 'now' });
    await done;
  });

  it('sends queued writes one after another, in order', async () => {
    const first = service.setHidden('agents', 'ast-1', true, []);
    const second = service.setHidden('agents', 'ast-2', true, []);
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

    const write = service.setHidden('agents', 'ast-1', true, ['ast-1']);
    await Promise.resolve();
    http.expectOne((r) => r.method === 'PUT').flush({}, { status: 503, statusText: 'Unavailable' });
    await expect(write).rejects.toBeTruthy();

    await answerGet({ sort: 'recent', order: [], hidden: [] });
    expect(service.hiddenIds('agents')().has('ast-1')).toBe(false);
  });

  it('sends a sort change with the page folded in, and an arrangement as Custom', async () => {
    const sorting = service.setSort('agents', 'custom', ['ast-2', 'ast-1']);
    await Promise.resolve();
    const sortPut = http.expectOne((r) => r.method === 'PUT');
    expect(sortPut.request.body).toEqual({ sort: 'custom', order: ['ast-2', 'ast-1'], hidden: [] });
    sortPut.flush({});
    await sorting;
    expect(service.arrangement('agents')().updatedAt).toBeTruthy();

    const saving = service.saveArranged('agents', ['ast-1', 'ast-2'], new Set(['ast-2']), ['ast-2', 'ast-1']);
    await Promise.resolve();
    const arrangePut = http.expectOne((r) => r.method === 'PUT');
    expect(arrangePut.request.body).toEqual({ sort: 'custom', order: ['ast-1', 'ast-2'], hidden: ['ast-2'] });
    arrangePut.flush({});
    await saving;
  });

  it('does nothing at all while the flag is off', async () => {
    ({ service, http } = setUp(false));
    service.open('agents');
    await service.setHidden('agents', 'ast-1', true, []);
    TestBed.tick();
    http.expectNone(() => true);
    expect(service.hiddenIds('agents')().size).toBe(0);
  });
});

describe('arrangeList', () => {
  const items = [{ id: 'a', name: 'beta' }, { id: 'b', name: 'Alpha' }, { id: 'c', name: 'gamma' }, { id: 'd', name: 'Delta' }];
  const options = (arrangement: Partial<typeof DEFAULT_ARRANGEMENT>) => ({
    idOf: (i: { id: string }) => i.id,
    nameOf: (i: { name: string }) => i.name,
    arrangement: { ...DEFAULT_ARRANGEMENT, ...arrangement },
  });
  const ids = (list: { id: string }[]) => list.map((i) => i.id);

  it('keeps the page order under Recently added, and splits hidden ones out', () => {
    const { visible, hidden, newIds } = arrangeList(items, options({ hidden: ['c', 'a', 'gone'] }));
    expect(ids(visible)).toEqual(['b', 'd']);
    expect(ids(hidden)).toEqual(['a', 'c']);
    expect(newIds.size).toBe(0);
  });

  it('sorts by name ignoring case', () => {
    expect(ids(arrangeList(items, options({ sort: 'name' })).visible)).toEqual(['b', 'a', 'd', 'c']);
  });

  it('puts new items first under Custom, then the saved order, and badges them once saved', () => {
    const saved = arrangeList(items, options({ sort: 'custom', order: ['c', 'a'], hidden: ['d'], updatedAt: 'then' }));
    expect(ids(saved.visible)).toEqual(['b', 'c', 'a']);
    expect(ids(saved.hidden)).toEqual(['d']);
    expect([...saved.newIds]).toEqual(['b']);

    const unsaved = arrangeList(items, options({ sort: 'custom', order: [], updatedAt: null }));
    expect(unsaved.newIds.size).toBe(0);
  });
});

describe('foldNew and withArranged', () => {
  it('folds new page ids to the top of a custom order, and leaves other sorts alone', () => {
    const custom = { ...DEFAULT_ARRANGEMENT, sort: 'custom' as const, order: ['a'], hidden: ['h'] };
    expect(foldNew(custom, ['n1', 'a', 'h', 'n2']).order).toEqual(['n1', 'n2', 'a']);
    const recent = { ...DEFAULT_ARRANGEMENT, order: ['a'] };
    expect(foldNew(recent, ['n1', 'a'])).toBe(recent);
  });

  it('saves the arranged order and hidden ids, keeping ids the page does not show', () => {
    const before = { ...DEFAULT_ARRANGEMENT, order: ['gone', 'a'], hidden: ['gone-hidden', 'a'] };
    const next = withArranged(before, ['b', 'a'], new Set(['b']), ['a', 'b']);
    expect(next.sort).toBe('custom');
    expect(next.order).toEqual(['b', 'a', 'gone']);
    expect(next.hidden).toEqual(['gone-hidden', 'b']);
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
