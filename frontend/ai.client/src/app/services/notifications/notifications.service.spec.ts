import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { InboxRefusal, NotificationsService } from './notifications.service';
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

  it('a read made elsewhere takes only the listed unread entries off the count', async () => {
    await load([notif('n3'), notif('n2'), notif('n1', '2026-09-24T01:00:00Z')], 2);
    service.markedElsewhere(n => n.notificationId === 'n3', 1);
    expect(service.unreadCount()).toBe(1);
    expect(service.notifications()[0].readAt).toBeTruthy();
    http.expectNone(`${BASE}?limit=20`);
  });

  it('an entry written since the last read is not taken off the count; a re-read settles it', async () => {
    // Dev, 2026-11-04: a handoff's entry landed after the bell's last read and was marked read in
    // view. Taking the server's count off the stale one showed 7 unread where the server said 8.
    await load([notif('n2'), notif('n1')], 2);
    service.markedElsewhere(n => n.notificationId === 'new', 1);
    expect(service.unreadCount()).toBe(2);
    http.expectOne(`${BASE}?limit=20`).flush({ notifications: [notif('n2'), notif('n1')], unreadCount: 2 });
    await new Promise(r => setTimeout(r, 0));
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

  describe('answering a paused run (answer-from-inbox §3.4–3.5)', () => {
    const paused = (id: string): AppNotification => ({
      ...notif(id), kind: 'agent_needs_approval', projectId: null, projectName: null, actorEmail: null,
      payload: { sessionId: 'headless-1', answerable: true, prompts: [{ interruptId: 'i1', kind: 'tool_approval' }] },
    });

    it('reads the live prompts without the error toast', async () => {
      const done = service.prompts('n1');
      const req = http.expectOne(`${BASE}/n1/prompts`);
      expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
      req.flush({ notificationId: 'n1', sessionId: 'headless-1', prompts: [{ interruptId: 'i1', kind: 'tool_approval' }] });
      expect((await done).prompts[0].interruptId).toBe('i1');
    });

    it('posts every answer at once, then shows the row claimed and read', async () => {
      await load([paused('n1'), notif('n2')], 2);
      const done = service.respond('n1', [{ interruptId: 'i1', response: 'approved' }]);
      const req = http.expectOne(`${BASE}/n1/respond`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ responses: [{ interruptId: 'i1', response: 'approved' }] });
      const resolution = { state: 'applying' as const, decision: 'approved' as const, at: '2026-11-26T12:00:00Z', resumeRunId: 'r-9' };
      req.flush({ resumeRunId: 'r-9', resolution }, { status: 202, statusText: 'Accepted' });
      expect((await done).resumeRunId).toBe('r-9');
      expect(service.notifications()[0].resolution).toEqual(resolution);
      expect(service.notifications()[0].readAt).toBe('2026-11-26T12:00:00Z');
      expect(service.unreadCount()).toBe(1);
    });

    it('a row given back to retry keeps its unread dot', async () => {
      await load([paused('n1')], 1);
      service.settled('n1', { state: 'retry', at: '2026-11-26T12:00:00Z' });
      expect(service.notifications()[0].readAt).toBeNull();
      expect(service.unreadCount()).toBe(1);
    });

    it.each([
      [409, { detail: { code: 'already_answered', message: 'This was already answered.' } }, 'already_answered', 'This was already answered.'],
      [410, { detail: { code: 'expired', message: 'This expired before it was answered.' } }, 'expired', 'This expired before it was answered.'],
      [422, { detail: { code: 'invalid_answers', message: 'Answer every prompt.' } }, 'invalid_answers', 'Answer every prompt.'],
      [503, { detail: { code: 'unavailable', message: 'Couldn’t send the answer. Try again.' } }, 'unavailable', 'Couldn’t send the answer. Try again.'],
      // The flag off, or not the caller's row: nothing waits here.
      [404, { detail: 'Notification not found' }, 'already_answered', 'This is no longer waiting for you.'],
      [500, { detail: 'boom' }, 'unavailable', 'Couldn’t reach the server. Try again.'],
    ])('a %i reads as %s', async (status, body, code, message) => {
      const done = service.respond('n1', [{ interruptId: 'i1', response: 'approved' }]);
      http.expectOne(`${BASE}/n1/respond`).flush(body, { status, statusText: 'Refused' });
      const refusal = await done.catch((e: unknown) => e);
      expect(refusal).toBeInstanceOf(InboxRefusal);
      expect((refusal as InboxRefusal).code).toBe(code);
      expect((refusal as InboxRefusal).message).toBe(message);
    });
  });
});
