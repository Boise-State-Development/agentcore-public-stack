import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { SessionService, SessionsListResponse, MessagesListResponse, BulkDeleteSessionsResponse } from './session.service';
import { AuthService } from '../../../auth/auth.service';
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
      imports: [HttpClientTestingModule],
      providers: [
        SessionService,
        { provide: AuthService, useValue: { isAuthenticated: vi.fn().mockReturnValue(false), ensureAuthenticated: vi.fn().mockResolvedValue(undefined) } },
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

    it('should clear cache', () => {
      service.addSessionToCache('s1', 'u1');
      service.addSessionToCache('s2', 'u1');
      service.clearSessionCache();
      expect(service.mergedSessionsResource().sessions).toHaveLength(0);
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
      const prefs = { lastModel: 'claude', lastTemperature: 0.7 };
      await service.updateSessionPreferences('test-id', prefs);
      expect(spy).toHaveBeenCalledWith('test-id', prefs);
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

  describe('updateSessionsParams', () => {
    it('should update params without error', () => {
      expect(() => service.updateSessionsParams({ limit: 20 })).not.toThrow();
    });
  });

  describe('resetSessionsParams', () => {
    it('should reset params without error', () => {
      service.updateSessionsParams({ limit: 20 });
      expect(() => service.resetSessionsParams()).not.toThrow();
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
