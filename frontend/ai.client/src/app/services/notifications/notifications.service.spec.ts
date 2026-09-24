import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { NotificationsService } from './notifications.service';
import { AppNotification } from './notification.model';
import { ConfigService } from '../config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';

const BASE = 'http://localhost:8000/notifications';

function notif(id: string, readAt: string | null = null): AppNotification {
  return {
    notificationId: id, recipientEmail: 'me@x.edu', kind: 'project_invited', projectId: 'prj_1',
    projectName: 'Enrollment Sync', actorEmail: 'ann@x.edu', payload: { role: 'editor' },
    createdAt: '2026-09-24T00:00:00Z', readAt,
  };
}

describe('NotificationsService', () => {
  let service: NotificationsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(NotificationsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function load(items: AppNotification[], unreadCount: number): Promise<void> {
    const done = service.refresh();
    const req = http.expectOne(`${BASE}?limit=20`);
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush({ notifications: items, unreadCount, nextCursor: null });
    await done;
  }

  it('loads the newest page and the unread count without the error toast', async () => {
    expect(service.ready()).toBe(false);
    await load([notif('n2'), notif('n1', '2026-09-24T01:00:00Z')], 1);
    expect(service.notifications().map(n => n.notificationId)).toEqual(['n2', 'n1']);
    expect(service.unreadCount()).toBe(1);
    expect(service.ready()).toBe(true);
  });

  it('marks one read optimistically, then tells the server', async () => {
    await load([notif('n1')], 1);
    const done = service.markRead(service.notifications()[0]);
    expect(service.unreadCount()).toBe(0);
    expect(service.notifications()[0].readAt).toBeTruthy();
    const req = http.expectOne(`${BASE}/n1/read`);
    expect(req.request.method).toBe('POST');
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush(null, { status: 204, statusText: 'No Content' });
    await done;
  });

  it('does not re-mark a read notification', async () => {
    await load([notif('n1', '2026-09-24T01:00:00Z')], 0);
    await service.markRead(service.notifications()[0]);
    http.expectNone(`${BASE}/n1/read`);
  });

  it('marks all read, and re-reads if the server refuses', async () => {
    await load([notif('n2'), notif('n1')], 2);
    const done = service.markAllRead();
    expect(service.unreadCount()).toBe(0);
    http.expectOne(`${BASE}/read-all`).flush({ detail: 'nope' }, { status: 500, statusText: 'Error' });
    await Promise.resolve();
    await new Promise(r => setTimeout(r, 0));
    http.expectOne(`${BASE}?limit=20`).flush({ notifications: [notif('n2'), notif('n1')], unreadCount: 2 });
    await done;
    expect(service.unreadCount()).toBe(2);
  });

  it('keeps what it had and says so when a read fails', async () => {
    await load([notif('n1')], 1);
    const done = service.refresh();
    http.expectOne(`${BASE}?limit=20`).flush({}, { status: 503, statusText: 'Unavailable' });
    await done;
    expect(service.error()).toContain('couldn’t be loaded');
    expect(service.notifications().length).toBe(1);
  });
});
