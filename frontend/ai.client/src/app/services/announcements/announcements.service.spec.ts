import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AnnouncementsService } from './announcements.service';
import { Announcement, AnnouncementFeed } from './announcement.model';
import { ConfigService } from '../config.service';

const API = 'http://localhost:8000';
const FEED_URL = `${API}/announcements/`;

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

function makeFeed(overrides: Partial<AnnouncementFeed> = {}): AnnouncementFeed {
  return {
    panel: [makeAnnouncement()],
    banner: null,
    modal: null,
    unread_count: 1,
    ...overrides,
  };
}

describe('AnnouncementsService', () => {
  let service: AnnouncementsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AnnouncementsService,
        // DI-token override rather than vi.mock, per house convention —
        // vi.mock pollutes across spec files.
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
      ],
    });
    service = TestBed.inject(AnnouncementsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({});
    });
    TestBed.resetTestingModule();
  });

  /** Trigger the resource loader and settle its request. */
  async function loadFeed(feed: AnnouncementFeed | 'error' = makeFeed()) {
    service.feedResource.reload();
    service.panelItems();
    await vi.waitFor(() => {
      const req = httpMock.expectOne(FEED_URL);
      if (feed === 'error') {
        req.flush('nope', { status: 500, statusText: 'Server Error' });
      } else {
        req.flush(feed);
      }
    });
    await vi.waitFor(() => {
      expect(service.feedResource.isLoading()).toBe(false);
    });
  }

  describe('feed', () => {
    it('exposes what the server sent', async () => {
      await loadFeed(
        makeFeed({
          panel: [makeAnnouncement({ announcement_id: 'a1' })],
          banner: makeAnnouncement({ announcement_id: 'a2', surfaces: ['panel', 'banner'] }),
          modal: makeAnnouncement({ announcement_id: 'a3', surfaces: ['panel', 'modal'] }),
          unread_count: 1,
        }),
      );

      expect(service.panelItems().map(a => a.announcement_id)).toEqual(['a1']);
      expect(service.bannerItem()?.announcement_id).toBe('a2');
      expect(service.modalItem()?.announcement_id).toBe('a3');
    });

    it('does not re-derive the caps client-side', async () => {
      // Five panel items, one banner — the server already capped it, and the
      // service must not second-guess that (§D5).
      const panel = ['a1', 'a2', 'a3', 'a4', 'a5'].map(id =>
        makeAnnouncement({ announcement_id: id, surfaces: ['panel', 'banner'] }),
      );
      await loadFeed(makeFeed({ panel, banner: panel[0], unread_count: 5 }));

      expect(service.panelItems()).toHaveLength(5);
      expect(service.bannerItem()?.announcement_id).toBe('a1');
    });

    it('degrades to an empty feed when the endpoint fails', async () => {
      // A 404 is the kill switch; a 500 is a bad day. Neither is worth an
      // error in front of the user for an ambient surface.
      await loadFeed('error');

      expect(service.panelItems()).toEqual([]);
      expect(service.unreadCount()).toBe(0);
      expect(service.hasUnread()).toBe(false);
    });
  });

  describe('unread count', () => {
    it('counts unread panel items', async () => {
      await loadFeed(
        makeFeed({
          panel: [
            makeAnnouncement({ announcement_id: 'a1', is_unread: true }),
            makeAnnouncement({ announcement_id: 'a2', is_unread: false }),
            makeAnnouncement({ announcement_id: 'a3', is_unread: true }),
          ],
        }),
      );

      expect(service.unreadCount()).toBe(2);
      expect(service.hasUnread()).toBe(true);
    });

    it('clears when the panel is marked seen, without waiting for the server', async () => {
      await loadFeed(
        makeFeed({
          panel: [
            makeAnnouncement({ announcement_id: 'a1' }),
            makeAnnouncement({ announcement_id: 'a2' }),
          ],
          unread_count: 2,
        }),
      );

      const done = service.markPanelSeen();
      // The dot is already gone; the POSTs are still in flight.
      expect(service.unreadCount()).toBe(0);

      // `match()` *consumes* what it returns, so take the pending requests
      // once and flush that same list — matching twice drains the queue and
      // leaves the promise hanging.
      const acks = httpMock.match(req => req.url.endsWith('/ack'));
      expect(acks).toHaveLength(2);
      acks.forEach(r => r.flush(null));
      await done;
    });

    it('sends no request when everything is already read', async () => {
      await loadFeed(
        makeFeed({
          panel: [makeAnnouncement({ is_unread: false })],
          unread_count: 0,
        }),
      );

      await service.markPanelSeen();
      httpMock.expectNone(req => req.url.endsWith('/ack'));
    });
  });

  describe('ack', () => {
    it('posts the action and surface', async () => {
      await loadFeed();

      const done = service.ack('a1', 'dismissed', 'banner');
      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${API}/announcements/a1/ack`);
        expect(req.request.method).toBe('POST');
        expect(req.request.body).toEqual({ action: 'dismissed', surface: 'banner' });
        req.flush(null);
      });

      expect(await done).toBe(true);
    });

    it('hides the item even when the ack fails — fail-open dismissal (§D7)', async () => {
      // A user trapped under a banner they cannot dismiss because of a
      // transient 500 is a worse outcome than one that comes back tomorrow.
      await loadFeed(
        makeFeed({
          banner: makeAnnouncement({ announcement_id: 'a1', surfaces: ['panel', 'banner'] }),
        }),
      );
      expect(service.bannerItem()).not.toBeNull();

      const done = service.ack('a1', 'dismissed', 'banner');
      await vi.waitFor(() => {
        httpMock
          .expectOne(`${API}/announcements/a1/ack`)
          .flush('nope', { status: 500, statusText: 'Server Error' });
      });

      expect(await done).toBe(false);
      expect(service.bannerItem()).toBeNull();
    });

    it('does not reject when the ack fails', async () => {
      await loadFeed();

      const done = service.ack('a1', 'dismissed', 'banner');
      await vi.waitFor(() => {
        httpMock
          .expectOne(`${API}/announcements/a1/ack`)
          .flush('nope', { status: 500, statusText: 'Server Error' });
      });

      // No caller should have to remember to catch this.
      await expect(done).resolves.toBe(false);
    });

    it('a local dismissal hides the modal too', async () => {
      await loadFeed(
        makeFeed({
          modal: makeAnnouncement({ announcement_id: 'a1', surfaces: ['panel', 'modal'] }),
        }),
      );

      const done = service.ack('a1', 'acknowledged', 'modal');
      await vi.waitFor(() => {
        httpMock.expectOne(`${API}/announcements/a1/ack`).flush(null);
      });
      await done;

      expect(service.modalItem()).toBeNull();
    });

    it('keeps the panel entry after a dismissal — the panel is the record (§D1)', async () => {
      await loadFeed(
        makeFeed({
          panel: [makeAnnouncement({ announcement_id: 'a1', surfaces: ['panel', 'banner'] })],
          banner: makeAnnouncement({ announcement_id: 'a1', surfaces: ['panel', 'banner'] }),
        }),
      );

      const done = service.ack('a1', 'dismissed', 'banner');
      await vi.waitFor(() => {
        httpMock.expectOne(`${API}/announcements/a1/ack`).flush(null);
      });
      await done;

      expect(service.bannerItem()).toBeNull();
      expect(service.panelItems()).toHaveLength(1);
    });

    it('a `seen` ack does not hide anything', async () => {
      await loadFeed(
        makeFeed({
          banner: makeAnnouncement({ announcement_id: 'a1', surfaces: ['panel', 'banner'] }),
        }),
      );

      const done = service.ack('a1', 'seen', 'banner');
      await vi.waitFor(() => {
        httpMock.expectOne(`${API}/announcements/a1/ack`).flush(null);
      });
      await done;

      // `seen` clears the dot and suppresses nothing (§D2).
      expect(service.bannerItem()).not.toBeNull();
      expect(service.unreadCount()).toBe(0);
    });
  });

  describe('isUnread', () => {
    it('reflects the server flag until acked locally', async () => {
      await loadFeed();
      const item = service.panelItems()[0];
      expect(service.isUnread(item)).toBe(true);

      const done = service.ack(item.announcement_id, 'seen', 'panel');
      expect(service.isUnread(item)).toBe(false);

      await vi.waitFor(() => {
        httpMock.expectOne(`${API}/announcements/a1/ack`).flush(null);
      });
      await done;
    });
  });
});
