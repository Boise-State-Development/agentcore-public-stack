import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { SessionService, SessionsListResponse, MessagesListResponse, BulkDeleteSessionsResponse, SIDEBAR_PAGE_SIZE } from './session.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';
import { SessionMetadata } from '../models/session-metadata.model';
import { Message } from '../models/message.model';

describe('SessionService', () => {
  let service: SessionService;
  let httpMock: HttpTestingController;

  const mockSession: SessionMetadata = {
    sessionId: 'test-session-id', userId: 'test-user-id', title: 'Test Session',
    status: 'active', createdAt: '2024-01-01T00:00:00Z', lastMessageAt: '2024-01-01T00:00:00Z', messageCount: 5,
  };

  const mockMessage: Message = { id: 'msg-1', role: 'user', content: [{ type: 'text', text: 'Hello' }] };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SessionService,
        { provide: BffSessionService, useValue: { isAuthenticated: signal(false) } },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(SessionService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock?.verify();
    TestBed.resetTestingModule();
  });

  describe('getSessions (no ensureAuthenticated)', () => {
    it('should GET sessions', async () => {
      const resp: SessionsListResponse = { sessions: [mockSession], nextToken: null };
      const promise = service.getSessions();
      httpMock.expectOne('http://localhost:8000/sessions').flush(resp);
      expect(await promise).toEqual(resp);
    });

    it('should pass query params', async () => {
      const promise = service.getSessions({ limit: 10, next_token: 'tok' });
      httpMock.expectOne('http://localhost:8000/sessions?limit=10&next_token=tok').flush({ sessions: [], nextToken: null });
      await promise;
    });
  });

  describe('getMessages (no ensureAuthenticated)', () => {
    it('should GET messages', async () => {
      const resp: MessagesListResponse = { messages: [mockMessage], nextToken: null };
      const promise = service.getMessages('s1');
      httpMock.expectOne('http://localhost:8000/sessions/s1/messages').flush(resp);
      expect(await promise).toEqual(resp);
    });
  });

  describe('getSessionMetadata', () => {
    it('should GET metadata after ensureAuthenticated', async () => {
      const promise = service.getSessionMetadata('test-session-id');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/test-session-id/metadata').flush(mockSession);
      });
      expect(await promise).toEqual(mockSession);
    });
  });

  describe('updateSessionMetadata', () => {
    it('should PUT metadata', async () => {
      const updated = { ...mockSession, title: 'Updated' };
      const promise = service.updateSessionMetadata('test-session-id', { title: 'Updated' });
      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/sessions/test-session-id/metadata');
        expect(req.request.method).toBe('PUT');
        req.flush(updated);
      });
      expect(await promise).toEqual(updated);
    });

    it('should update currentSession when sessionId matches', async () => {
      service.currentSession.set(mockSession);
      const updated = { ...mockSession, title: 'Updated' };
      const promise = service.updateSessionMetadata('test-session-id', { title: 'Updated' });
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/test-session-id/metadata').flush(updated);
      });
      const result = await promise;
      expect(result.title).toBe('Updated');
    });
  });

  describe('updateSessionTitle', () => {
    it('should delegate to updateSessionMetadata', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      await service.updateSessionTitle('s1', 'New');
      expect(spy).toHaveBeenCalledWith('s1', { title: 'New' });
    });
  });

  describe('deleteSession', () => {
    it('should DELETE session', async () => {
      const promise = service.deleteSession('test-session-id');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/test-session-id').flush({});
      });
      await promise;
    });

    it('should clear currentSession if matches', async () => {
      service.currentSession.set(mockSession);
      const promise = service.deleteSession('test-session-id');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/test-session-id').flush({});
      });
      await promise;
      expect(service.currentSession().sessionId).toBe('');
    });
  });

  describe('bulkDeleteSessions', () => {
    it('should POST bulk-delete', async () => {
      const resp: BulkDeleteSessionsResponse = { deletedCount: 2, failedCount: 0, results: [{ sessionId: 's1', success: true }, { sessionId: 's2', success: true }] };
      const promise = service.bulkDeleteSessions(['s1', 's2']);
      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/sessions/bulk-delete');
        expect(req.request.body).toEqual({ sessionIds: ['s1', 's2'] });
        req.flush(resp);
      });
      expect(await promise).toEqual(resp);
    });
  });

  describe('local cache', () => {
    it('should add session to cache', () => {
      service.addSessionToCache('new-id', 'user-1', 'New');
      const sessions = service.mergedSessionsResource().sessions;
      expect(sessions.length).toBe(1);
      expect(sessions[0].sessionId).toBe('new-id');
    });

    it('should track new sessions', () => {
      service.addSessionToCache('new-id', 'user-1');
      expect(service.isNewSession('new-id')).toBe(true);
      expect(service.isNewSession('other')).toBe(false);
    });

    it('should update title in cache', () => {
      service.addSessionToCache('s1', 'u1', 'Old');
      service.updateSessionTitleInCache('s1', 'New');
      expect(service.mergedSessionsResource().sessions[0].title).toBe('New');
      expect(service.isNewSession('s1')).toBe(false);
    });

    it('takes preferences from the API row for an optimistic row that has none', () => {
      // A task started in this tab is cached before the backend binds it to the
      // project; without this the sidebar could not group it until a reload.
      const local: SessionMetadata = { ...mockSession, sessionId: 's1', title: 'Local title' };
      const api: SessionMetadata = {
        ...mockSession, sessionId: 's1', title: 'Api title',
        preferences: { assistantId: 'ast-1', projectId: 'prj_1' },
      };
      const merged: SessionMetadata[] = (service as any).mergeSessions([local], [api, { ...mockSession, sessionId: 's2' }]);
      expect(merged.map(s => s.sessionId)).toEqual(['s1', 's2']);
      expect(merged[0].title).toBe('Local title');
      expect(merged[0].preferences?.projectId).toBe('prj_1');

      const withOwn: SessionMetadata = { ...local, preferences: { assistantId: 'mine' } };
      expect((service as any).mergeSessions([withOwn], [api])[0].preferences).toEqual({ assistantId: 'mine' });
    });

    it('should clear cache', () => {
      service.addSessionToCache('s1', 'u1');
      service.addSessionToCache('s2', 'u1');
      service.clearSessionCache();
      expect(service.mergedSessionsResource().sessions).toHaveLength(0);
    });
  });

  describe('applyServerTitle', () => {
    it('should update the cache and currentSession when the session is active', () => {
      service.addSessionToCache('s1', 'u1', 'New Conversation');
      service.currentSession.set({ ...mockSession, sessionId: 's1', title: 'New Conversation' });

      service.applyServerTitle('s1', 'Generated Title');

      expect(service.mergedSessionsResource().sessions[0].title).toBe('Generated Title');
      expect(service.currentSession().title).toBe('Generated Title');
      expect(service.isNewSession('s1')).toBe(false);
    });

    it('should leave currentSession alone when another session is active', () => {
      service.addSessionToCache('s1', 'u1', 'New Conversation');
      service.currentSession.set({ ...mockSession, sessionId: 'other', title: 'Other Title' });

      service.applyServerTitle('s1', 'Generated Title');

      expect(service.mergedSessionsResource().sessions[0].title).toBe('Generated Title');
      expect(service.currentSession().title).toBe('Other Title');
    });
  });

  describe('enableSessionsLoading / disableSessionsLoading', () => {
    it('should toggle without error', () => {
      expect(() => service.enableSessionsLoading()).not.toThrow();
      expect(() => service.disableSessionsLoading()).not.toThrow();
    });
  });

  describe('toggleStarred', () => {
    it('should delegate to updateSessionMetadata with starred true', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      await service.toggleStarred('test-id', true);
      expect(spy).toHaveBeenCalledWith('test-id', { starred: true });
    });

    it('should delegate to updateSessionMetadata with starred false', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      await service.toggleStarred('test-id', false);
      expect(spy).toHaveBeenCalledWith('test-id', { starred: false });
    });
  });

  describe('updateSessionTags', () => {
    it('should delegate to updateSessionMetadata with tags', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      const tags = ['tag1', 'tag2'];
      await service.updateSessionTags('test-id', tags);
      expect(spy).toHaveBeenCalledWith('test-id', { tags });
    });
  });

  describe('updateSessionStatus', () => {
    it('should delegate to updateSessionMetadata with status', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      await service.updateSessionStatus('test-id', 'archived');
      expect(spy).toHaveBeenCalledWith('test-id', { status: 'archived' });
    });
  });

  describe('updateSessionPreferences', () => {
    it('should delegate to updateSessionMetadata with preferences', async () => {
      const spy = vi.spyOn(service, 'updateSessionMetadata').mockResolvedValue(mockSession);
      const prefs = { lastModel: 'claude' };
      await service.updateSessionPreferences('test-id', prefs);
      expect(spy).toHaveBeenCalledWith('test-id', prefs);
    });
  });

  describe('markSessionRead / markSessionUnread', () => {
    it('markSessionRead POSTs /read, sets a read watermark, and clears currentSession.unread', async () => {
      service.currentSession.set({ ...mockSession, unread: true });

      const promise = service.markSessionRead({ ...mockSession, unread: true });
      const req = httpMock.expectOne('http://localhost:8000/sessions/test-session-id/read');
      expect(req.request.method).toBe('POST');
      req.flush(null);
      await promise;

      expect(service.isLocallyRead(mockSession)).toBe(true);
      expect(service.currentSession().unread).toBe(false);
    });

    it('markSessionUnread POSTs /unread, lifts the read watermark, and sets currentSession.unread', async () => {
      service.currentSession.set({ ...mockSession, unread: false });
      // Seed a read watermark first so we can prove the unread path lifts it.
      const readPromise = service.markSessionRead(mockSession);
      httpMock.expectOne('http://localhost:8000/sessions/test-session-id/read').flush(null);
      await readPromise;
      expect(service.isLocallyRead(mockSession)).toBe(true);

      const promise = service.markSessionUnread(mockSession);
      const req = httpMock.expectOne('http://localhost:8000/sessions/test-session-id/unread');
      expect(req.request.method).toBe('POST');
      req.flush(null);
      await promise;

      expect(service.isLocallyRead(mockSession)).toBe(false);
      expect(service.currentSession().unread).toBe(true);
    });
  });

  describe('hasCurrentSession', () => {
    it('should return true when sessionId is set', () => {
      service.currentSession.set({ ...mockSession, sessionId: 'test-id' });
      expect(service.hasCurrentSession()).toBe(true);
    });

    it('should return false when sessionId is empty', () => {
      service.currentSession.set({ ...mockSession, sessionId: '' });
      expect(service.hasCurrentSession()).toBe(false);
    });
  });

  describe('setSessionMetadataId', () => {
    it('should set sessionMetadataId without error', () => {
      expect(() => service.setSessionMetadataId('test-id')).not.toThrow();
      expect(() => service.setSessionMetadataId(null)).not.toThrow();
    });
  });

  describe('sidebar paging', () => {
    const LIST = 'http://localhost:8000/sessions';
    const session = (n: number): SessionMetadata => ({ ...mockSession, sessionId: `s${n}` });
    const range = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => session(from + i));

    /** Answer the list request matching `url` once the resource's effect has issued it. */
    async function flushList(url: string, body: SessionsListResponse): Promise<void> {
      await vi.waitFor(() => {
        TestBed.tick();
        httpMock.expectOne(url).flush(body);
      });
      await vi.waitFor(() => expect(service.sessionsResource.isLoading()).toBe(false));
    }

    beforeEach(() => {
      // Signed in, or the auth effect's logout branch disables loading on the first tick.
      (TestBed.inject(BffSessionService).isAuthenticated as ReturnType<typeof signal<boolean>>).set(true);
    });

    async function loadFirstPage(): Promise<void> {
      service.enableSessionsLoading();
      await flushList(`${LIST}?limit=${SIDEBAR_PAGE_SIZE}`, { sessions: range(0, 30), nextToken: 'p2' });
    }

    it('first load asks for one page, not the whole history', async () => {
      await loadFirstPage();
      expect(service.mergedSessionsResource().sessions).toHaveLength(30);
    });

    it('appends the next page and carries its cursor', async () => {
      await loadFirstPage();
      const more = service.loadMoreSessions();
      httpMock.expectOne(`${LIST}?limit=30&next_token=p2`).flush({ sessions: range(30, 60), nextToken: 'p3' });
      await more;

      const merged = service.mergedSessionsResource();
      expect(merged.sessions.map(s => s.sessionId)).toEqual(range(0, 60).map(s => s.sessionId));
      expect(merged.nextToken).toBe('p3');
    });

    it('reloads the whole loaded window, so a reload never drops appended rows', async () => {
      await loadFirstPage();
      const more = service.loadMoreSessions();
      httpMock.expectOne(`${LIST}?limit=30&next_token=p2`).flush({ sessions: range(30, 60), nextToken: 'p3' });
      await more;

      service.refreshSessions();
      await flushList(`${LIST}?limit=60`, { sessions: range(0, 60), nextToken: 'p3' });
      expect(service.mergedSessionsResource().sessions).toHaveLength(60);
    });

    it('drops a page that a reload overtook, rather than aborting the reload', async () => {
      await loadFirstPage();
      const more = service.loadMoreSessions();
      service.refreshSessions();
      TestBed.tick();
      httpMock.expectOne(`${LIST}?limit=30&next_token=p2`).flush({ sessions: range(30, 60), nextToken: 'p3' });
      await more;

      await flushList(`${LIST}?limit=30`, { sessions: range(0, 30), nextToken: 'p2' });
      expect(service.mergedSessionsResource().sessions).toHaveLength(30);
      expect(service.mergedSessionsResource().nextToken).toBe('p2');
    });

    it('does nothing once the list is exhausted', async () => {
      service.enableSessionsLoading();
      await flushList(`${LIST}?limit=30`, { sessions: range(0, 5), nextToken: null });
      await service.loadMoreSessions();
      httpMock.expectNone(req => req.url === LIST);
    });

    it('flags a failed page and clears the flag on the next attempt', async () => {
      await loadFirstPage();
      const failed = service.loadMoreSessions();
      httpMock.expectOne(`${LIST}?limit=30&next_token=p2`).flush('boom', { status: 500, statusText: 'Server Error' });
      await failed;
      expect(service.loadMoreSessionsError()).toBe(true);
      expect(service.isLoadingMoreSessions()).toBe(false);

      const retry = service.loadMoreSessions();
      expect(service.loadMoreSessionsError()).toBe(false);
      httpMock.expectOne(`${LIST}?limit=30&next_token=p2`).flush({ sessions: range(30, 40), nextToken: null });
      await retry;
      expect(service.mergedSessionsResource().sessions).toHaveLength(40);
    });
  });

  describe('deleteSession - newSessionIds removal', () => {
    it('should remove session from newSessionIds after delete', async () => {
      service.addSessionToCache('id2', 'user-1');
      expect(service.isNewSession('id2')).toBe(true);
      const promise = service.deleteSession('id2');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/id2').flush({});
      });
      await promise;
      expect(service.isNewSession('id2')).toBe(false);
    });
  });

  describe('bulkDeleteSessions - currentSession clearing', () => {
    it('should clear currentSession if it was deleted', async () => {
      service.currentSession.set({ ...mockSession, sessionId: 'current-id' });
      const resp: BulkDeleteSessionsResponse = { deletedCount: 2, failedCount: 0, results: [{ sessionId: 'current-id', success: true }, { sessionId: 'other-id', success: true }] };
      const promise = service.bulkDeleteSessions(['current-id', 'other-id']);
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/sessions/bulk-delete').flush(resp);
      });
      await promise;
      expect(service.currentSession().sessionId).toBe('');
    });
  });
});
