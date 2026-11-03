import { TestBed } from '@angular/core/testing';
import {
  PREFLIGHT_DISMISSALS_KEY,
  PreflightDismissalStorageService,
} from './preflight-dismissal-storage.service';

describe('PreflightDismissalStorageService', () => {
  let storage: PreflightDismissalStorageService;

  function fresh(): PreflightDismissalStorageService {
    TestBed.resetTestingModule();
    return TestBed.inject(PreflightDismissalStorageService);
  }

  function stored(): unknown {
    const raw = localStorage.getItem(PREFLIGHT_DISMISSALS_KEY);
    return raw ? JSON.parse(raw) : null;
  }

  beforeEach(() => {
    localStorage.removeItem(PREFLIGHT_DISMISSALS_KEY);
    storage = fresh();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
    localStorage.removeItem(PREFLIGHT_DISMISSALS_KEY);
  });

  it('persists a dismissal under the signed-in user', () => {
    storage.adopt('u-1');
    storage.add('oracle-oac');

    expect(stored()).toEqual({ owner: 'u-1', providers: ['oracle-oac'] });
    const reloaded = fresh();
    reloaded.adopt('u-1');
    expect(reloaded.has('oracle-oac')).toBe(true);
  });

  it('drops another user\'s dismissals on adopt', () => {
    storage.adopt('u-1');
    storage.add('oracle-oac');

    const next = fresh();
    next.adopt('u-2');

    expect(next.has('oracle-oac')).toBe(false);
    expect(stored()).toBeNull();
  });

  it('forget() removes one provider and the entry once empty', () => {
    storage.adopt('u-1');
    storage.add('oracle-oac');
    storage.add('github');

    storage.forget('oracle-oac');
    expect(stored()).toEqual({ owner: 'u-1', providers: ['github'] });

    storage.forget('github');
    expect(stored()).toBeNull();
  });

  it('sees a dismissal another tab wrote', () => {
    storage.adopt('u-1');
    localStorage.setItem(
      PREFLIGHT_DISMISSALS_KEY,
      JSON.stringify({ owner: 'u-1', providers: ['github'] }),
    );

    expect(storage.has('github')).toBe(true);
  });

  it('clear() forgets everything', () => {
    storage.adopt('u-1');
    storage.add('oracle-oac');

    storage.clear();

    expect(storage.has('oracle-oac')).toBe(false);
    expect(stored()).toBeNull();
  });

  it('ignores a corrupt entry', () => {
    localStorage.setItem(PREFLIGHT_DISMISSALS_KEY, '{not json');
    storage.adopt('u-1');

    expect(storage.has('oracle-oac')).toBe(false);
  });

  it('falls back to memory when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    storage.adopt('u-1');
    storage.add('oracle-oac');

    expect(storage.has('oracle-oac')).toBe(true);
  });

  it('holds a dismissal in memory before any user is adopted', () => {
    storage.add('oracle-oac');

    expect(storage.has('oracle-oac')).toBe(true);
    expect(stored()).toBeNull();
  });
});
