import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';

import { ArtifactLibraryPage } from './artifact-library.page';
import {
  ArtifactHttpService,
  type LibraryArtifact,
} from '../session/services/artifacts/artifact-http.service';
import {
  ArtifactShareService,
  type SharedWithMeArtifact,
} from '../session/services/artifacts/artifact-share.service';
import { LocalSettingsService, type ViewMode } from '../services/local-settings.service';
import { ToastService } from '../services/toast/toast.service';

function stubShared(
  overrides: Partial<SharedWithMeArtifact> = {},
): SharedWithMeArtifact {
  return {
    shareId: 'sh-1',
    title: 'Their deck',
    contentType: 'text/html',
    version: 1,
    ownerEmail: 'ada@x.com',
    sharedAt: '2026-06-01T10:00:00+00:00',
    shareUrl: '/shared-artifact/sh-1',
    ...overrides,
  };
}

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
    renameArtifact: ReturnType<typeof vi.fn>;
    deleteArtifact: ReturnType<typeof vi.fn>;
  };
  /** Stands in for the CDK dialog. `closed` is what the page awaits, so
   *  each test sets the value the user "chose" before acting. */
  let mockShares: { listSharedWithMe: ReturnType<typeof vi.fn> };
  let mockDialog: { open: ReturnType<typeof vi.fn> };
  let dialogResult: unknown;
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
      renameArtifact: vi.fn(),
      deleteArtifact: vi.fn().mockResolvedValue(undefined),
    };
    // Default: the inbox does not exist in this environment (the flag is
    // off), which is what every pre-existing test in this file assumes.
    mockShares = { listSharedWithMe: vi.fn().mockResolvedValue(null) };
    dialogResult = undefined;
    mockDialog = {
      open: vi.fn(() => ({ closed: of(dialogResult) })),
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
          { path: 'artifacts/:artifactId', children: [] },
        ]),
        { provide: ArtifactHttpService, useValue: mockHttp },
        { provide: ArtifactShareService, useValue: mockShares },
        { provide: LocalSettingsService, useValue: mockSettings },
        { provide: ToastService, useValue: mockToast },
        { provide: Dialog, useValue: mockDialog },
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
      filtered: () => Array<{
        key: string;
        kind: 'owned' | 'shared';
        title: string;
        route: readonly string[];
        ownerEmail?: string;
        timestampLabel: string;
      }>;
      typeOptions: () => Array<{ value: string; label: string }>;
      isEmpty: () => boolean;
      isFilteredEmpty: () => boolean;
      isTabEmpty: () => boolean;
      emptyTabMessage: () => string;
      error: () => string | null;
      loading: () => boolean;
      search: { set: (v: string) => void };
      typeFilter: { set: (v: string) => void };
      styleFor: (t: string) => { label: string };
      setViewMode: (m: ViewMode) => void;
      open: (row: { route: readonly string[] }) => void;
      tab: () => 'all' | 'yours' | 'shared';
      setTab: (t: 'all' | 'yours' | 'shared') => void;
      sharedAvailable: () => boolean;
      received: () => SharedWithMeArtifact[] | null;
      receivedCursor: () => string | null;
      loadMoreShared: () => Promise<void>;
      totalCount: () => number;
      tabCount: () => number;
      load: () => Promise<void>;
      rename: (item: LibraryArtifact) => Promise<void>;
      confirmDelete: (item: LibraryArtifact) => Promise<void>;
      busy: () => string | null;
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
    // Keys are prefixed by provenance — an artifact id and a share id
    // come from different key spaces and could otherwise collide.
    expect(c.filtered().map((r) => r.key)).toEqual(['owned:b', 'owned:a']);
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
    expect(c.filtered().map((r) => r.key)).toEqual(['owned:a']);
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
    expect(c.filtered().map((r) => r.key)).toEqual(['owned:a']);
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
    /**
     * The library used to open artifacts with `window.open`, which made
     * viewing your own document contingent on a pop-up — refusable by any
     * blocker, and refused unconditionally by embedded webviews. Opening
     * is now in-app navigation, which nothing can block. These tests pin
     * that down so nobody reintroduces the pop-up dependency.
     */
    it('navigates to the in-app viewer instead of opening a window', async () => {
      const openSpy = vi.spyOn(window, 'open');
      const navigate = vi
        .spyOn(TestBed.inject(Router), 'navigate')
        .mockResolvedValue(true);

      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ artifactId: 'a1' }),
      ]);
      const c = api(await createComponent());
      // Opened through the row the page actually built, so the route
      // under test is the projection's, not the spec's.
      c.open(c.filtered()[0]);

      expect(navigate).toHaveBeenCalledWith(['/artifacts', 'a1']);
      expect(openSpy).not.toHaveBeenCalled();
    });

    it('does not mint a render token — the viewer owns that', async () => {
      vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());
      c.open(c.filtered()[0]);

      expect(mockHttp.mintRenderToken).not.toHaveBeenCalled();
    });

    it('cannot fail in a way the user has to be told about', async () => {
      // A route change has no blocked/failed path to report, so the old
      // "Pop-up blocked" and "Could not open artifact" toasts are gone.
      vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());
      c.open(c.filtered()[0]);

      expect(mockToast.error).not.toHaveBeenCalled();
    });
  });

  describe('shared with you', () => {
    /** Turn the inbox on with the given rows. */
    function withInbox(
      artifacts: SharedWithMeArtifact[],
      nextCursor: string | null = null,
    ): void {
      mockShares.listSharedWithMe.mockResolvedValue({
        artifacts,
        nextCursor,
      });
    }

    it('hides the tabs entirely when the inbox does not exist', async () => {
      // `null` is the backend saying the endpoint 404'd — the feature is
      // off in this environment. A lone "Yours" tab would be chrome
      // around nothing.
      mockShares.listSharedWithMe.mockResolvedValue(null);
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());

      expect(c.sharedAvailable()).toBe(false);
      expect(c.filtered()).toHaveLength(1);
    });

    it('distinguishes an absent inbox from an empty one', async () => {
      // Collapsing these would either show a permanently empty tab
      // wherever the feature is off, or hide a real empty state.
      withInbox([]);
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());

      expect(c.sharedAvailable()).toBe(true);
      expect(c.received()).toEqual([]);
    });

    it('merges both lists newest-first on the All tab', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({
          artifactId: 'mine',
          updatedAt: '2026-06-05T10:00:00+00:00',
        }),
      ]);
      withInbox([
        stubShared({ shareId: 'theirs', sharedAt: '2026-06-10T10:00:00+00:00' }),
      ]);
      const c = api(await createComponent());

      // Two independently server-ordered lists can only be shown as one
      // by interleaving them here; the tabs that show one list each do
      // not sort at all.
      expect(c.filtered().map((r) => r.key)).toEqual([
        'shared:theirs',
        'owned:mine',
      ]);
    });

    it('scopes each tab to its own list', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ artifactId: 'mine' })]);
      withInbox([stubShared({ shareId: 'theirs' })]);
      const c = api(await createComponent());

      c.setTab('yours');
      expect(c.filtered().map((r) => r.key)).toEqual(['owned:mine']);

      c.setTab('shared');
      expect(c.filtered().map((r) => r.key)).toEqual(['shared:theirs']);
    });

    it('routes a received artifact to the recipient page', async () => {
      // A recipient has no artifact id and no owner route — the share id
      // is the only handle they have on it.
      mockHttp.listLibrary.mockResolvedValue([]);
      withInbox([stubShared({ shareId: 'sh-9' })]);
      const c = api(await createComponent());

      const row = c.filtered()[0];
      expect(row.kind).toBe('shared');
      expect(row.route).toEqual(['/shared-artifact', 'sh-9']);
      expect(row.ownerEmail).toBe('ada@x.com');
      // "Updated" is the owner's clock and means nothing to a recipient.
      expect(row.timestampLabel).toBe('Shared');
    });

    it('searches received artifacts by sender as well as title', async () => {
      mockHttp.listLibrary.mockResolvedValue([]);
      withInbox([
        stubShared({ shareId: 'a', title: 'Deck', ownerEmail: 'ada@x.com' }),
        stubShared({ shareId: 'b', title: 'Notes', ownerEmail: 'bob@x.com' }),
      ]);
      const c = api(await createComponent());

      // "Who sent me that thing" is at least as likely a starting point
      // as remembering what it was called.
      c.search.set('ada');
      expect(c.filtered().map((r) => r.key)).toEqual(['shared:a']);
    });

    it('keeps your library when the inbox request fails', async () => {
      // The library is the page's reason to exist; a 503 on the inbox
      // must not blank it. The tabs simply do not appear.
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      mockShares.listSharedWithMe.mockRejectedValue(new Error('503'));
      const c = api(await createComponent());

      expect(c.error()).toBeNull();
      expect(c.filtered()).toHaveLength(1);
      expect(c.sharedAvailable()).toBe(false);
    });

    it('still fails the page when your own library fails', async () => {
      mockHttp.listLibrary.mockRejectedValue(new Error('503'));
      withInbox([stubShared()]);
      const c = api(await createComponent());

      expect(c.error()).toContain("couldn't load");
    });

    it('appends the next page rather than replacing it', async () => {
      mockHttp.listLibrary.mockResolvedValue([]);
      withInbox([stubShared({ shareId: 'first' })], 'cursor-1');
      const c = api(await createComponent());

      expect(c.receivedCursor()).toBe('cursor-1');

      mockShares.listSharedWithMe.mockResolvedValue({
        artifacts: [stubShared({ shareId: 'second' })],
        nextCursor: null,
      });
      await c.loadMoreShared();

      expect(mockShares.listSharedWithMe).toHaveBeenLastCalledWith('cursor-1');
      expect(c.filtered().map((r) => r.key)).toEqual([
        'shared:first',
        'shared:second',
      ]);
      expect(c.receivedCursor()).toBeNull();
    });

    it('drops the tabs if the inbox disappears while paging', async () => {
      // A deploy or a flag flip mid-session. Leaving a "Load more"
      // button that does nothing is worse than losing the tab.
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      withInbox([stubShared()], 'cursor-1');
      const c = api(await createComponent());

      mockShares.listSharedWithMe.mockResolvedValue(null);
      await c.loadMoreShared();

      expect(c.sharedAvailable()).toBe(false);
      expect(c.receivedCursor()).toBeNull();
    });

    it('falls back to All when the selected tab stops existing', async () => {
      withInbox([stubShared()]);
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());
      c.setTab('shared');

      mockShares.listSharedWithMe.mockResolvedValue(null);
      await c.load();

      expect(c.tab()).toBe('all');
      expect(c.filtered()).toHaveLength(1);
    });

    it('counts against the tab, not the whole library', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ artifactId: 'a' }),
        stubArtifact({ artifactId: 'b' }),
      ]);
      withInbox([stubShared()]);
      const c = api(await createComponent());

      expect(c.totalCount()).toBe(3);
      c.setTab('shared');
      // "n of m" has to follow the tab or it reads as a bug.
      expect(c.tabCount()).toBe(1);
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

    it('previews the artifact on grid cards but not on list rows', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);

      const listFixture = await createComponent();
      // A row is a scanning surface; a preview per row would spend a render
      // Lambda invocation each on a picture 24px tall.
      expect(
        listFixture.nativeElement.querySelector('app-artifact-thumbnail'),
      ).toBeNull();
      listFixture.destroy();

      mockSettings.artifactsViewMode.set('grid');
      const gridFixture = await createComponent();
      const thumbnail = gridFixture.nativeElement.querySelector(
        'app-artifact-thumbnail',
      );
      expect(thumbnail).not.toBeNull();
      // The preview opens the same artifact as the title and the Open button,
      // so it stays out of the accessibility tree rather than becoming a third
      // route to one destination.
      expect(thumbnail.closest('[aria-hidden="true"]')).not.toBeNull();
    });

    it('does not blame a search the user never made', async () => {
      // Regression: opening "Shared with you" with nothing shared used to
      // show "No artifacts match your search" because the empty-state
      // gate read the LIBRARY total rather than the tab's. Found on dev.
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      mockShares.listSharedWithMe.mockResolvedValue({
        artifacts: [],
        nextCursor: null,
      });
      const c = api(await createComponent());
      c.setTab('shared');

      expect(c.isFilteredEmpty()).toBe(false);
      expect(c.isTabEmpty()).toBe(true);
      expect(c.emptyTabMessage()).toContain('shared with you');
    });

    it('still blames the search when there really was one', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ title: 'Budget model' }),
      ]);
      mockShares.listSharedWithMe.mockResolvedValue({
        artifacts: [],
        nextCursor: null,
      });
      const c = api(await createComponent());
      c.setTab('yours');
      c.search.set('nothing matches this');

      expect(c.isTabEmpty()).toBe(false);
      expect(c.isFilteredEmpty()).toBe(true);
    });

    it('reports an empty library ahead of an empty tab', async () => {
      // With nothing anywhere, "No artifacts yet" is the true statement;
      // a per-tab message would bury the one that matters.
      mockHttp.listLibrary.mockResolvedValue([]);
      mockShares.listSharedWithMe.mockResolvedValue({
        artifacts: [],
        nextCursor: null,
      });
      const c = api(await createComponent());
      c.setTab('shared');

      expect(c.isEmpty()).toBe(true);
      expect(c.isTabEmpty()).toBe(false);
      expect(c.isFilteredEmpty()).toBe(false);
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

  describe('rename', () => {
    it('patches the row from the response, not from what was typed', async () => {
      // The server trims. Echoing the typed value back would let the page
      // drift from what is actually stored.
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ artifactId: 'a' })]);
      mockHttp.renameArtifact.mockResolvedValue(
        stubArtifact({ artifactId: 'a', title: 'Trimmed' }),
      );
      const c = api(await createComponent());
      dialogResult = '  Trimmed  ';

      await c.rename(c.items()[0]);

      expect(mockHttp.renameArtifact).toHaveBeenCalledWith('a', '  Trimmed  ');
      expect(c.items()[0].title).toBe('Trimmed');
    });

    it('does nothing when the dialog is cancelled', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());
      dialogResult = undefined;

      await c.rename(c.items()[0]);

      expect(mockHttp.renameArtifact).not.toHaveBeenCalled();
    });

    it('keeps the old title and warns when the request fails', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ title: 'Quarterly plan' }),
      ]);
      mockHttp.renameArtifact.mockRejectedValue(new Error('503'));
      const c = api(await createComponent());
      dialogResult = 'New name';

      await c.rename(c.items()[0]);

      expect(c.items()[0].title).toBe('Quarterly plan');
      expect(mockToast.error).toHaveBeenCalled();
      expect(c.busy()).toBeNull();
    });
  });

  describe('delete', () => {
    it('removes the row only after the request succeeds', async () => {
      mockHttp.listLibrary.mockResolvedValue([
        stubArtifact({ artifactId: 'a' }),
        stubArtifact({ artifactId: 'b' }),
      ]);
      const c = api(await createComponent());
      dialogResult = true;

      await c.confirmDelete(c.items()[0]);

      expect(mockHttp.deleteArtifact).toHaveBeenCalledWith('a');
      expect(c.items().map((i) => i.artifactId)).toEqual(['b']);
    });

    it('does nothing when the confirmation is declined', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ artifactId: 'a' })]);
      const c = api(await createComponent());
      dialogResult = false;

      await c.confirmDelete(c.items()[0]);

      expect(mockHttp.deleteArtifact).not.toHaveBeenCalled();
      expect(c.items()).toHaveLength(1);
    });

    it('confirms destructively, naming versions and shares', async () => {
      // Neither is visible from this page, so the dialog copy is the only
      // place a user learns a share link dies with the artifact.
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const c = api(await createComponent());
      dialogResult = false;

      await c.confirmDelete(c.items()[0]);

      const data = mockDialog.open.mock.calls[0][1].data;
      expect(data.destructive).toBe(true);
      expect(data.message).toContain('every version');
      expect(data.message).toContain('share links');
    });

    it('keeps the row and warns when the request fails', async () => {
      mockHttp.listLibrary.mockResolvedValue([stubArtifact({ artifactId: 'a' })]);
      mockHttp.deleteArtifact.mockRejectedValue(new Error('503'));
      const c = api(await createComponent());
      dialogResult = true;

      await c.confirmDelete(c.items()[0]);

      expect(c.items()).toHaveLength(1);
      expect(mockToast.error).toHaveBeenCalled();
      expect(c.busy()).toBeNull();
    });
  });

  describe('grid card footer', () => {
    it('keeps both button labels in the DOM when they collapse', async () => {
      // The footer carries four controls and the card is ~13rem wide at
      // three columns on a 1080px window, so a container query drops the
      // "Open" / "Conversation" text below 19rem. It must be dropped to
      // `sr-only`, never `hidden`: these buttons have no aria-label, so
      // removing the text would leave them with no accessible name at
      // exactly the width where they become bare icons.
      mockSettings.artifactsViewMode.set('grid');
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const fixture = await createComponent();
      const host = fixture.nativeElement as HTMLElement;

      const labels = [...host.querySelectorAll('ul.grid span')].filter((el) =>
        ['Open', 'Conversation'].includes((el.textContent ?? '').trim()),
      );
      expect(labels).toHaveLength(2);
      for (const label of labels) {
        expect(label.className).toContain('@max-[19rem]:sr-only');
        expect(label.className).not.toContain('hidden');
      }
    });

    it('gives the collapsing buttons a tooltip to stand in for the text', async () => {
      mockSettings.artifactsViewMode.set('grid');
      mockHttp.listLibrary.mockResolvedValue([stubArtifact()]);
      const fixture = await createComponent();
      const host = fixture.nativeElement as HTMLElement;

      const open = [...host.querySelectorAll('ul.grid button')].find((b) =>
        (b.textContent ?? '').includes('Open'),
      )!;
      // TooltipDirective binds no attribute of its own, so assert via the
      // directive instance the template wired up.
      const de = fixture.debugElement
        .queryAll((n) => n.nativeElement === open)
        .at(0);
      expect(de?.attributes['appTooltipPosition']).toBe('top');
    });
  });
});
