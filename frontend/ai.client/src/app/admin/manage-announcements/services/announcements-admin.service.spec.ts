import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ConfigService } from '../../../services/config.service';
import { AnnouncementsService } from '../../../services/announcements/announcements.service';
import { AnnouncementsAdminService } from './announcements-admin.service';
import { Announcement } from '../models/announcement.model';

const API = 'http://api.test';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills',
    summary: null,
    surfaces: ['panel'],
    severity: 'info',
    state: 'published',
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

describe('AnnouncementsAdminService — reach', () => {
  let service: AnnouncementsAdminService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: AnnouncementsService, useValue: { reload: vi.fn() } },
      ],
    });
    service = TestBed.inject(AnnouncementsAdminService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  function statsUrl(id: string) {
    return `${API}/admin/announcements/${id}/stats`;
  }

  it('fetches reach for a published announcement', async () => {
    const pending = service.loadStats([makeAnnouncement()]);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 1,
      seen: 12,
      dismissed: 8,
      acknowledged: 3,
      targeted: 40,
    });
    await pending;

    expect(service.statsFor('a1')?.seen).toBe(12);
  });

  it('does not ask about a draft — nothing has been shown', async () => {
    await service.loadStats([makeAnnouncement({ state: 'draft' })]);
    http.expectNone(statsUrl('a1'));
    expect(service.statsFor('a1')).toBeNull();
  });

  it('asks once per announcement, however often the list re-renders', async () => {
    const announcements = [makeAnnouncement()];
    const first = service.loadStats(announcements);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 1,
      seen: 1,
      dismissed: 0,
      acknowledged: 0,
      targeted: null,
    });
    await first;

    await service.loadStats(announcements);
    http.expectNone(statsUrl('a1'));
  });

  it('re-asks after a revision bump — the counters restart', async () => {
    // "Show again" starts a fresh count, so a cached entry from the previous
    // revision would report stale reach for a broadcast that just went out.
    const first = service.loadStats([makeAnnouncement({ revision: 1 })]);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 1,
      seen: 9,
      dismissed: 9,
      acknowledged: 0,
      targeted: null,
    });
    await first;

    const second = service.loadStats([makeAnnouncement({ revision: 2 })]);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 2,
      seen: 1,
      dismissed: 0,
      acknowledged: 0,
      targeted: null,
    });
    await second;

    expect(service.statsFor('a1')?.revision).toBe(2);
    expect(service.statsFor('a1')?.seen).toBe(1);
  });

  it('fails soft — a broken stats endpoint leaves the list usable', async () => {
    const pending = service.loadStats([makeAnnouncement()]);
    http.expectOne(statsUrl('a1')).flush('boom', {
      status: 500,
      statusText: 'Server Error',
    });

    await expect(pending).resolves.toBeUndefined();
    expect(service.statsFor('a1')).toBeNull();
  });

  it('retries a failed fetch on the next pass rather than caching the failure', async () => {
    const first = service.loadStats([makeAnnouncement()]);
    http.expectOne(statsUrl('a1')).flush('boom', {
      status: 500,
      statusText: 'Server Error',
    });
    await first;

    const second = service.loadStats([makeAnnouncement()]);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 1,
      seen: 4,
      dismissed: 0,
      acknowledged: 0,
      targeted: null,
    });
    await second;

    expect(service.statsFor('a1')?.seen).toBe(4);
  });

  it('drops cached reach when a mutation lands', async () => {
    const first = service.loadStats([makeAnnouncement()]);
    http.expectOne(statsUrl('a1')).flush({
      announcement_id: 'a1',
      revision: 1,
      seen: 5,
      dismissed: 0,
      acknowledged: 0,
      targeted: null,
    });
    await first;
    expect(service.statsFor('a1')).not.toBeNull();

    const archived = service.archive('a1');
    http.expectOne(`${API}/admin/announcements/a1/archive`).flush(
      makeAnnouncement({ state: 'archived' }),
    );
    await archived;

    // Publishing/archiving/revising all change what reach means.
    expect(service.statsFor('a1')).toBeNull();
  });
});
