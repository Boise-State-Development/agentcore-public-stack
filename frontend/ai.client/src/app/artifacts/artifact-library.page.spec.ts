import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';

import { ArtifactLibraryPage } from './artifact-library.page';
import {
  ArtifactHttpService,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import { LocalSettingsService, type ViewMode } from '../services/local-settings.service';
import { ToastService } from '../services/toast/toast.service';

function stubArtifact(overrides: Partial<LibraryArtifact> = {}): LibraryArtifact {
  return {
    artifactId: 'a1',
    version: 1,
    title: 'Quarterly plan',
    contentType: 'text/markdown',
    createdAt: '2026-05-01T09:00:00+00:00',
    updatedAt: '2026-05-15T10:00:00+00:00',
    sessionId: 'sess-1',
    ...overrides,
  };
}

describe('ArtifactLibraryPage', () => {
  let mockHttp: {
    listLibrary: ReturnType<typeof vi.fn>;
    mintRenderToken: ReturnType<typeof vi.fn>;
  };
  let mockSettings: {
    artifactsViewMode: ReturnType<typeof signal<ViewMode>>;
    setArtifactsViewMode: ReturnType<typeof vi.fn>;
  };
  let mockToast: { success: ReturnType<typeof vi.fn>; error: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockHttp = {
      listLibrary: vi.fn().mockResolvedValue([]),
      mintRenderToken: vi
        .fn()
        .mockResolvedValue({ url: 'https://artifacts.example/x?t=tok', expiresAt: '' }),
    };
    mockSettings = {
      artifactsViewMode: signal<ViewMode>('list'),
      setArtifactsViewMode: vi.fn(),
    };
    mockToast = { success: vi.fn(), error: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        // The rows link to /s/:sessionId; an empty router would make those
        // links throw NG04002 during template rendering.
        provideRouter([
          { path: '', children: [] },
          { path: 's/:sessionId', children: [] },
        ]),
        { provide: ArtifactHttpService, useValue: mockHttp },
        { provide: LocalSettingsService, useValue: mockSettings },
        { provide: ToastService, useValue: mockToast },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  async function createComponent() {
    const fixture = TestBed.createComponent(ArtifactLibraryPage);
    fixture.detectChanges();
    // The constructor's load() is async; flush it before asserting.
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
    return fixture;
  }

  // Reaching protected members the way the template does.
  function api(fixture: Awaited<ReturnType<typeof createComponent>>) {
    return fixture.componentInstance as unknown as {
      items: () => LibraryArtifact[];
      filtered: () => LibraryArtifact[];
      typeOptions: () => Array<{ value: string; label: string }>;
      isEmpty: () => boolean;
      isFilteredEmpty: () => boolean;
      error: () => string | null;
      loading: () => boolean;
      search: { set: (v: string) => void };
      typeFilter: { set: (v: string) => void };
      styleFor: (t: string) => { label: string };
      setViewMode: (m: ViewMode) => void;
      open: (item: LibraryArtifact) => Promise<void>;
      load: () => Promise<void>;
    };
  }

  it('loads the library on init', async () => {
    await createComponent();
    expect(mockHttp.listLibrary).toHaveBeenCalledTimes(1);
  });

  it('preserves server ordering rather than re-sorting', async () => {
    // Deliberately not in date order: the server sorts, the client must not
    // second-guess it, or the two will diverge once paging exists.
    mockHttp.listLibrary.mockResolvedValue([
      stubArtifact({ artifactId: 'b', updatedAt: '2026-01-01T00:00:00+00:00' }),
      stubArtifact({ artifactId: 'a', updatedAt: '2026-09-01T00:00:00+00:00' }),
    ]);
    const c = api(await createComponent());
    expect(c.filtered().map((i) => i.artifactId)).toEqual(['b', 'a']);
  });

  it('surfaces a retryable message when the load fails', async () => {
    mockHttp.listLibrary.mockRejectedValue(new Error('503'));
    const c = api(await createComponent());
    expect(c.error()).toContain("couldn't load");
    expect(c.loading()).toBe(false);
  });

  it('filters by title, case-insensitively', async () => {
    mockHttp.listLibrary.mockResolvedValue([
      stubArtifact({ artifactId: 'a', title: 'Budget model' }),
      stubArtifact({ artifactId: 'b', title: 'Reading list' }),
    ]);
    const c = api(await createComponent());
    c.search.set('BUDGET');
    expect(c.filtered().map((i) => i.artifactId)).toEqual(['a']);
  });

  it('filters by type with the charset suffix normalized away', async () => {
    // The writer stores HTML as `text/html; charset=utf-8`. A filter keyed on
    // the raw string would never match it.
    mockHttp.listLibrary.mockResolvedValue([
      stubArtifact({ artifactId: 'a', contentType: 'text/html; charset=utf-8' }),
      stubArtifact({ artifactId: 'b', contentType: 'text/markdown' }),
    ]);
    const c = api(await createComponent());
    c.typeFilter.set('text/html');
    expect(c.filtered().map((i) => i.artifactId)).toEqual(['a']);
  });

  it('offers only the types the user actually has, deduped', async () => {
    mockHttp.listLibrary.mockResolvedValue([
      stubArtifact({ artifactId: 'a', contentType: 'text/markdown' }),
      stubArtifact({ artifactId: 'b', contentType: 'text/markdown' }),
      stubArtifact({ artifactId: 'c', contentType: 'text/csv' }),
    ]);
    const c = api(await createComponent());
    expect(c.typeOptions().map((o) => o.label)).toEqual(['CSV', 'Markdown']);
  });

  it('labels an unrecognised content type instead of dropping it', async () => {
    mockHttp.listLibrary.mockResolvedValue([
      stubArtifact({ contentType: 'application/x-weird' }),
    ]);
    const c = api(await createComponent());
    expect(c.filtered()).toHaveLength(1);
    expect(c.styleFor('application/x-weird').label).toBe('Text');
  });

  it('distinguishes an empty library from an empty filter result', async () => {
    // Conflating these tells someone with a full library that it is empty.
    mockHttp.listLibrary.mockResolvedValue([stubArtifact({ title: 'Budget' })]);
    const c = api(await createComponent());

    expect(c.isEmpty()).toBe(false);
    expect(c.isFilteredEmpty()).toBe(false);

    c.search.set('nothing matches this');
    expect(c.isEmpty()).toBe(false);
    expect(c.isFilteredEmpty()).toBe(true);
  });

  it('reports an empty library only once loading has resolved', async () => {
    mockHttp.listLibrary.mockResolvedValue([]);
    const c = api(await createComponent());
    expect(c.isEmpty()).toBe(true);
  });

  it('persists the view mode through local settings', async () => {
    const c = api(await createComponent());
    c.setViewMode('grid');
    expect(mockSettings.setArtifactsViewMode).toHaveBeenCalledWith('grid');
  });

  describe('open()', () => {
    it('opens the tab before awaiting the mint, then points it at the URL', async () => {
      // The ordering is the whole contract: a window.open() after the await
      // is no longer tied to the click gesture and every browser blocks it.
      const tab = { location: { href: '' }, close: vi.fn() };
      const openSpy = vi
        .spyOn(window, 'open')
        .mockImplementation(() => tab as unknown as Window);

      let mintStarted = false;
      mockHttp.mintRenderToken.mockImplementation(async () => {
        mintStarted = true;
        expect(openSpy).toHaveBeenCalled();
        return { url: 'https://artifacts.example/a1', expiresAt: '' };
      });

      const c = api(await createComponent());
      await c.open(stubArtifact({ artifactId: 'a1', version: 3, sessionId: 's9' }));

      expect(mintStarted).toBe(true);
      expect(mockHttp.mintRenderToken).toHaveBeenCalledWith('a1', 3, 's9');
      expect(tab.location.href).toBe('https://artifacts.example/a1');
      expect(tab.close).not.toHaveBeenCalled();
    });

    it('closes the blank tab and reports the failure when minting fails', async () => {
      const tab = { location: { href: '' }, close: vi.fn() };
      vi.spyOn(window, 'open').mockImplementation(() => tab as unknown as Window);
      mockHttp.mintRenderToken.mockRejectedValue(new Error('500'));

      const c = api(await createComponent());
      await c.open(stubArtifact());

      // Leaving an about:blank tab open would look like a broken app.
      expect(tab.close).toHaveBeenCalled();
      expect(mockToast.error).toHaveBeenCalled();
    });

    it('reports a blocked pop-up without minting a token', async () => {
      vi.spyOn(window, 'open').mockImplementation(() => null);

      const c = api(await createComponent());
      await c.open(stubArtifact());

      expect(mockHttp.mintRenderToken).not.toHaveBeenCalled();
      expect(mockToast.error).toHaveBeenCalledWith(
        'Pop-up blocked',
        expect.stringContaining('Allow pop-ups'),
      );
    });
  });

  describe('rendering', () => {
    it('renders a row per artifact in list view', async () => {
      mockSettings.artifactsViewMode.set('list');
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ artifactId: 'a', title: 'Budget model' }),
        stubArtifact({ artifactId: 'b', title: 'Reading list' }),
      ]);
      const fixture = await createComponent();
      const rows = fixture.nativeElement.querySelectorAll('li');
      expect(rows).toHaveLength(2);
      expect(fixture.nativeElement.textContent).toContain('Budget model');
    });

    it('switches to cards in grid view', async () => {
      mockSettings.artifactsViewMode.set('grid');
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ title: 'Budget model' })]);
      const fixture = await createComponent();
      // The grid card exposes a labelled Open control; the list row uses an
      // icon button with an sr-only label instead.
      expect(fixture.nativeElement.textContent).toContain('Open');
      expect(fixture.nativeElement.textContent).toContain('Conversation');
    });

    it('falls back to a placeholder title and an undated label', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ title: '', updatedAt: '' }),
      ]);
      const fixture = await createComponent();
      expect(fixture.nativeElement.textContent).toContain('Untitled artifact');
      expect(fixture.nativeElement.textContent).toContain('Date unknown');
    });

    it('omits the conversation link when the row has no session', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ sessionId: '' })]);
      const fixture = await createComponent();
      expect(fixture.nativeElement.querySelector('a[href^="/s/"]')).toBeNull();
    });
  });
});
