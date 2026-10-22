import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ManageAnnouncementsPage } from './manage-announcements.page';
import { AnnouncementsAdminService } from './services/announcements-admin.service';
import {
  Announcement,
  AnnouncementState,
  AnnouncementStats,
} from './models/announcement.model';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills',
    summary: null,
    surfaces: ['panel'],
    severity: 'info',
    state: 'draft',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: null,
    target_roles: ['*'],
    show_to_new_users: false,
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    created_by: 'admin@example.com',
    ...overrides,
  };
}

function makeStats(overrides: Partial<AnnouncementStats> = {}): AnnouncementStats {
  return {
    announcement_id: 'a1',
    revision: 1,
    seen: 12,
    dismissed: 8,
    acknowledged: 3,
    targeted: 40,
    ...overrides,
  };
}

describe('ManageAnnouncementsPage', () => {
  let items: ReturnType<typeof signal<Announcement[]>>;
  let statsById: ReturnType<typeof signal<Map<string, AnnouncementStats>>>;
  let service: any;
  let confirmSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    items = signal<Announcement[]>([]);
    statsById = signal<Map<string, AnnouncementStats>>(new Map());
    service = {
      ensureLoaded: vi.fn(),
      announcements: items,
      loadStats: vi.fn(async () => undefined),
      statsFor: (id: string) => statsById().get(id) ?? null,
      announcementsResource: { isLoading: () => false, error: () => null },
      publish: vi.fn(async () => makeAnnouncement({ state: 'published' })),
      archive: vi.fn(async () => makeAnnouncement({ state: 'archived' })),
      revise: vi.fn(async () => makeAnnouncement({ revision: 2 })),
      remove: vi.fn(async () => undefined),
    };
    TestBed.configureTestingModule({
      providers: [{ provide: AnnouncementsAdminService, useValue: service }],
    });
    confirmSpy = vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
    TestBed.resetTestingModule();
  });

  function createPage() {
    return TestBed.runInInjectionContext(() => new ManageAnnouncementsPage()) as any;
  }

  it('loads the admin list on construction', () => {
    createPage();
    expect(service.ensureLoaded).toHaveBeenCalled();
  });

  describe('publish affordance', () => {
    it.each<[AnnouncementState, boolean]>([
      ['draft', true],
      ['scheduled', true],
      ['published', false],
      // Archived is terminal — the server 400s, so do not offer the button.
      ['archived', false],
    ])('state %s → publishable: %s', (state, expected) => {
      const page = createPage();
      expect(page.canPublish(makeAnnouncement({ state }))).toBe(expected);
    });
  });

  describe('destructive actions confirm first', () => {
    it('asks before bumping the revision, and says what it does', async () => {
      // "Show again" re-surfaces the announcement for everyone who dismissed
      // it — the one thing an admin fixing a typo must not trigger by accident.
      const page = createPage();
      await page.onRevise(makeAnnouncement());

      expect(confirmSpy).toHaveBeenCalledOnce();
      expect(confirmSpy.mock.calls[0][0]).toContain('dismissed it will see it once more');
      expect(service.revise).toHaveBeenCalledWith('a1');
    });

    it('does not revise when the confirm is declined', async () => {
      confirmSpy.mockReturnValue(false);
      const page = createPage();
      await page.onRevise(makeAnnouncement());

      expect(service.revise).not.toHaveBeenCalled();
    });

    it('asks before archiving and mentions acks are kept', async () => {
      const page = createPage();
      await page.onArchive(makeAnnouncement());

      expect(confirmSpy.mock.calls[0][0]).toContain('acknowledgements are kept');
      expect(service.archive).toHaveBeenCalledWith('a1');
    });

    it('asks before deleting and points at archive instead', async () => {
      const page = createPage();
      await page.onDelete(makeAnnouncement());

      expect(confirmSpy.mock.calls[0][0]).toContain('Archive instead');
      expect(service.remove).toHaveBeenCalledWith('a1');
    });

    it('publishes without a confirm — it is reversible by archiving', async () => {
      const page = createPage();
      await page.onPublish(makeAnnouncement());

      expect(confirmSpy).not.toHaveBeenCalled();
      expect(service.publish).toHaveBeenCalledWith('a1');
    });
  });

  it('surfaces the server detail when an action fails', async () => {
    service.publish = vi.fn(async () => {
      throw { error: { detail: "cannot publish an announcement in state 'archived'" } };
    });
    const page = createPage();
    await page.onPublish(makeAnnouncement());

    expect(page.actionError()).toContain('cannot publish');
    expect(page.busyId()).toBeNull();
  });

  describe('row summary', () => {
    it('describes an untargeted announcement as Everyone', () => {
      items.set([makeAnnouncement()]);
      const page = createPage();
      expect(page.rows()[0].audience).toBe('Everyone');
    });

    it('lists the roles when targeted', () => {
      items.set([makeAnnouncement({ target_roles: ['faculty', 'staff'] })]);
      const page = createPage();
      expect(page.rows()[0].audience).toBe('faculty, staff');
    });

    it('calls out showToNewUsers, since it is the surprising setting', () => {
      items.set([makeAnnouncement({ show_to_new_users: true })]);
      const page = createPage();
      expect(page.rows()[0].audience).toContain('including users who join later');
    });

    it('says "Live since" for a published announcement and "Publishes" otherwise', () => {
      items.set([
        makeAnnouncement({ announcement_id: 'live', state: 'published' }),
        makeAnnouncement({ announcement_id: 'draft', state: 'draft' }),
      ]);
      const page = createPage();
      expect(page.rows()[0].timing).toContain('Live since');
      expect(page.rows()[1].timing).toContain('Publishes');
    });

    it('mentions the expiry when there is one', () => {
      items.set([makeAnnouncement({ expires_at: '2099-01-01T00:00:00Z' })]);
      const page = createPage();
      expect(page.rows()[0].timing).toContain('expires');
    });

    it('renders a dash rather than "Invalid Date" for junk', () => {
      items.set([makeAnnouncement({ publish_at: 'not a date' })]);
      const page = createPage();
      expect(page.rows()[0].timing).toContain('—');
    });

    it('tolerates the legacy +00:00Z timestamp spelling', () => {
      items.set([makeAnnouncement({ publish_at: '2026-03-04T00:00:00+00:00Z' })]);
      const page = createPage();
      expect(page.rows()[0].timing).not.toContain('—');
    });
  });

  describe('surface chips', () => {
    it('maps each surface to its own icon', () => {
      const page = createPage();
      expect(page.surfaceIcon('panel')).toBe('heroWindow');
      expect(page.surfaceIcon('banner')).toBe('heroRectangleGroup');
      expect(page.surfaceIcon('modal')).toBe('heroBellAlert');
    });

    it('gives every state a distinct chip style', () => {
      const page = createPage();
      const states: AnnouncementState[] = ['draft', 'scheduled', 'published', 'archived'];
      const classes = states.map(s => page.stateChipClass(s));
      expect(new Set(classes).size).toBe(states.length);
    });
  });

  describe('reach', () => {
    it('says nothing for a draft — zeroes would read as "nobody engaged"', () => {
      items.set([makeAnnouncement({ state: 'draft' })]);
      statsById.set(new Map([['a1', makeStats()]]));
      const page = createPage();
      expect(page.rows()[0].reach).toBeNull();
    });

    it('says nothing until the stats have arrived', () => {
      items.set([makeAnnouncement({ state: 'published' })]);
      const page = createPage();
      expect(page.rows()[0].reach).toBeNull();
    });

    it('renders the funnel once stats land', () => {
      items.set([makeAnnouncement({ state: 'published' })]);
      const page = createPage();
      statsById.set(new Map([['a1', makeStats()]]));

      const reach = page.rows()[0].reach!;
      expect(reach).toContain('12 seen');
      expect(reach).toContain('8 dismissed');
      expect(reach).toContain('~40 targeted');
      expect(reach).toContain('estimate');
    });

    it('omits acknowledged unless one was ever asked for', () => {
      items.set([
        makeAnnouncement({ state: 'published', requires_ack: false }),
      ]);
      const page = createPage();
      statsById.set(new Map([['a1', makeStats()]]));
      expect(page.rows()[0].reach).not.toContain('acknowledged');
    });

    it('shows acknowledged for a requiresAck announcement', () => {
      items.set([makeAnnouncement({ state: 'published', requires_ack: true })]);
      const page = createPage();
      statsById.set(new Map([['a1', makeStats()]]));
      expect(page.rows()[0].reach).toContain('3 acknowledged');
    });

    it('says the audience is not estimated rather than implying zero', () => {
      // A role-scoped announcement cannot be counted — `targeted` is null, and
      // rendering that as "of ~0" would read as nobody being targeted.
      items.set([
        makeAnnouncement({ state: 'published', target_roles: ['faculty'] }),
      ]);
      const page = createPage();
      statsById.set(new Map([['a1', makeStats({ targeted: null })]]));

      const reach = page.rows()[0].reach!;
      expect(reach).toContain('audience not estimated');
      expect(reach).not.toContain('~0');
    });

    it('still reports reach for an archived announcement', () => {
      // Archiving stops it showing but keeps the acks — the record is the
      // reason to archive rather than delete.
      items.set([makeAnnouncement({ state: 'archived' })]);
      const page = createPage();
      statsById.set(new Map([['a1', makeStats()]]));
      expect(page.rows()[0].reach).toContain('12 seen');
    });

    it('requests stats once the list resolves', () => {
      items.set([makeAnnouncement({ state: 'published' })]);
      createPage();
      // The fetch is an effect, so it needs a flush — the page is constructed
      // directly here rather than through a fixture.
      TestBed.tick();
      expect(service.loadStats).toHaveBeenCalled();
    });
  });
});
