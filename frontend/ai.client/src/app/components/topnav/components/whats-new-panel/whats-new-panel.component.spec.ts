import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DialogRef } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { AnnouncementsService } from '../../../../services/announcements/announcements.service';
import { Announcement } from '../../../../services/announcements/announcement.model';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills',
    summary: null,
    surfaces: ['panel'],
    severity: 'info',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: null,
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    is_unread: true,
    is_updated: false,
    ...overrides,
  };
}

describe('WhatsNewPanelComponent', () => {
  let panelItems: ReturnType<typeof signal<Announcement[]>>;
  let readIds: Set<string>;
  let mockAnnouncements: any;
  let mockDialogRef: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    panelItems = signal<Announcement[]>([]);
    readIds = new Set<string>();
    mockAnnouncements = {
      panelItems,
      // Mirrors the real service: `markPanelSeen` clears unread immediately.
      isUnread: (a: Announcement) => a.is_unread && !readIds.has(a.announcement_id),
      markPanelSeen: vi.fn(async () => {
        for (const a of panelItems()) readIds.add(a.announcement_id);
      }),
    };
    mockDialogRef = { close: vi.fn() };

    // DI-token overrides rather than vi.mock, per house convention.
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: mockAnnouncements },
        { provide: DialogRef, useValue: mockDialogRef },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { WhatsNewPanelComponent } = await import('./whats-new-panel.component');
    return TestBed.runInInjectionContext(() => new WhatsNewPanelComponent());
  }

  function itemsOf(component: any) {
    return component.items() as Array<{ announcement: Announcement; pill: string | null }>;
  }

  it('marks the panel seen on open — that is what clears the unread dot', async () => {
    panelItems.set([makeAnnouncement()]);
    await createComponent();

    expect(mockAnnouncements.markPanelSeen).toHaveBeenCalledTimes(1);
  });

  it('keeps the pills visible after marking seen', async () => {
    // The unread state is snapshotted at open. Reading it live would make
    // every pill vanish the instant the dialog appeared, so the user would
    // never learn which entries were new to them.
    panelItems.set([makeAnnouncement({ is_unread: true })]);
    const component = await createComponent();

    expect(mockAnnouncements.isUnread(panelItems()[0])).toBe(false);
    expect(itemsOf(component)[0].pill).toBe('New');
  });

  it('says "Updated" when the admin bumped the revision on something already read', async () => {
    panelItems.set([makeAnnouncement({ is_unread: true, is_updated: true, revision: 2 })]);
    const component = await createComponent();

    expect(itemsOf(component)[0].pill).toBe('Updated');
  });

  it('shows no pill on an announcement that was already read', async () => {
    panelItems.set([makeAnnouncement({ is_unread: false })]);
    const component = await createComponent();

    expect(itemsOf(component)[0].pill).toBeNull();
  });

  it('renders an empty list without calling the ack path', async () => {
    panelItems.set([]);
    const component = await createComponent();

    expect(itemsOf(component)).toEqual([]);
    expect(mockAnnouncements.markPanelSeen).toHaveBeenCalledTimes(1);
  });

  it('closes through the DialogRef', async () => {
    const component = await createComponent();
    (component as any).onClose();

    expect(mockDialogRef.close).toHaveBeenCalled();
  });

  describe('published label', () => {
    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-06-10T12:00:00Z'));
    });
    afterEach(() => vi.useRealTimers());

    async function labelFor(publishAt: string): Promise<string> {
      panelItems.set([makeAnnouncement({ publish_at: publishAt })]);
      const component = await createComponent();
      return (component as any).items()[0].publishedLabel;
    }

    it('reads "Today" for something published hours ago', async () => {
      expect(await labelFor('2026-06-10T09:00:00Z')).toBe('Today');
    });

    it('reads "Yesterday" for the day before', async () => {
      expect(await labelFor('2026-06-09T09:00:00Z')).toBe('Yesterday');
    });

    it('counts days inside the last week', async () => {
      expect(await labelFor('2026-06-07T12:00:00Z')).toBe('3 days ago');
    });

    it('falls back to a date beyond a week', async () => {
      expect(await labelFor('2026-05-01T12:00:00Z')).toBe('May 1');
    });

    it('tolerates the legacy `+00:00Z` timestamp spelling', async () => {
      // Rows written before the timestamp fix keep that spelling forever;
      // `new Date()` returns Invalid Date for it, so the label would be blank.
      expect(await labelFor('2026-06-09T09:00:00+00:00Z')).toBe('Yesterday');
    });

    it('renders nothing rather than "Invalid Date" for junk', async () => {
      expect(await labelFor('not a date')).toBe('');
    });
  });
});
