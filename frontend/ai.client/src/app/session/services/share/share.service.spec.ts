import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ShareService, ShareResponse, SharedConversationResponse, ShareListResponse, ExportResponse } from './share.service';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';

describe('ShareService', () => {
  let service: ShareService;
  let httpMock: HttpTestingController;
  const baseUrl = 'http://localhost:8000';

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ShareService,
        { provide: AuthService, useValue: { ensureAuthenticated: vi.fn().mockResolvedValue(undefined) } },
        { provide: ConfigService, useValue: { appApiUrl: signal(baseUrl) } },
      ],
    });

    service = TestBed.inject(ShareService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock?.verify();
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  // -----------------------------------------------------------------------
  // createShare
  // -----------------------------------------------------------------------

  describe('createShare', () => {
    it('should POST to /conversations/{sessionId}/share with public access', async () => {
      const mockResponse: ShareResponse = {
        shareId: 'share-001',
        sessionId: 'sess-001',
        ownerId: 'user-001',
        accessLevel: 'public',
        createdAt: '2025-06-01T00:00:00Z',
        shareUrl: '/shared/share-001',
      };

      const promise = service.createShare('sess-001', 'public');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/conversations/sess-001/share`);
        expect(req.request.method).toBe('POST');
        expect(req.request.body).toEqual({ accessLevel: 'public' });
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.shareId).toBe('share-001');
      expect(result.accessLevel).toBe('public');
    });

    it('should include allowedEmails when provided', async () => {
      const mockResponse: ShareResponse = {
        shareId: 'share-002',
        sessionId: 'sess-001',
        ownerId: 'user-001',
        accessLevel: 'specific',
        allowedEmails: ['owner@test.com', 'friend@test.com'],
        createdAt: '2025-06-01T00:00:00Z',
        shareUrl: '/shared/share-002',
      };

      const promise = service.createShare('sess-001', 'specific', ['friend@test.com']);

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/conversations/sess-001/share`);
        expect(req.request.body).toEqual({
          accessLevel: 'specific',
          allowedEmails: ['friend@test.com'],
        });
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.accessLevel).toBe('specific');
    });

    it('should call ensureAuthenticated before making request', async () => {
      const authService = TestBed.inject(AuthService);
      const mockResponse: ShareResponse = {
        shareId: 'share-001',
        sessionId: 'sess-001',
        ownerId: 'user-001',
        accessLevel: 'public',
        createdAt: '2025-06-01T00:00:00Z',
        shareUrl: '/shared/share-001',
      };

      const promise = service.createShare('sess-001', 'public');

      await vi.waitFor(() => {
        httpMock.expectOne(`${baseUrl}/conversations/sess-001/share`).flush(mockResponse);
      });

      await promise;
      expect(authService.ensureAuthenticated).toHaveBeenCalled();
    });
  });

  // -----------------------------------------------------------------------
  // getSharedConversation
  // -----------------------------------------------------------------------

  describe('getSharedConversation', () => {
    it('should GET /shared/{shareId}', async () => {
      const mockResponse: SharedConversationResponse = {
        shareId: 'share-001',
        title: 'Test Conversation',
        accessLevel: 'public',
        createdAt: '2025-06-01T00:00:00Z',
        ownerId: 'user-001',
        messages: [],
      };

      const promise = service.getSharedConversation('share-001');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/shared/share-001`);
        expect(req.request.method).toBe('GET');
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.shareId).toBe('share-001');
      expect(result.title).toBe('Test Conversation');
    });
  });

  // -----------------------------------------------------------------------
  // listSharesForSession
  // -----------------------------------------------------------------------

  describe('listSharesForSession', () => {
    it('should GET /conversations/{sessionId}/shares', async () => {
      const mockResponse: ShareListResponse = {
        shares: [
          {
            shareId: 'share-001',
            sessionId: 'sess-001',
            ownerId: 'user-001',
            accessLevel: 'public',
            createdAt: '2025-06-01T00:00:00Z',
            shareUrl: '/shared/share-001',
          },
          {
            shareId: 'share-002',
            sessionId: 'sess-001',
            ownerId: 'user-001',
            accessLevel: 'specific',
            allowedEmails: ['user@test.com'],
            createdAt: '2025-06-02T00:00:00Z',
            shareUrl: '/shared/share-002',
          },
        ],
      };

      const promise = service.listSharesForSession('sess-001');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/conversations/sess-001/shares`);
        expect(req.request.method).toBe('GET');
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.shares.length).toBe(2);
    });
  });

  // -----------------------------------------------------------------------
  // revokeShare (now by shareId)
  // -----------------------------------------------------------------------

  describe('revokeShare', () => {
    it('should DELETE /shares/{shareId}', async () => {
      const promise = service.revokeShare('share-001');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/shares/share-001`);
        expect(req.request.method).toBe('DELETE');
        req.flush(null);
      });

      await promise;
    });
  });

  // -----------------------------------------------------------------------
  // updateShare (now by shareId)
  // -----------------------------------------------------------------------

  describe('updateShare', () => {
    it('should PATCH /shares/{shareId} with new access level', async () => {
      const mockResponse: ShareResponse = {
        shareId: 'share-001',
        sessionId: 'sess-001',
        ownerId: 'user-001',
        accessLevel: 'public',
        createdAt: '2025-06-01T00:00:00Z',
        shareUrl: '/shared/share-001',
      };

      const promise = service.updateShare('share-001', 'public');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/shares/share-001`);
        expect(req.request.method).toBe('PATCH');
        expect(req.request.body).toEqual({ accessLevel: 'public' });
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.accessLevel).toBe('public');
    });

    it('should include allowedEmails when updating to specific', async () => {
      const mockResponse: ShareResponse = {
        shareId: 'share-001',
        sessionId: 'sess-001',
        ownerId: 'user-001',
        accessLevel: 'specific',
        allowedEmails: ['owner@test.com', 'new@test.com'],
        createdAt: '2025-06-01T00:00:00Z',
        shareUrl: '/shared/share-001',
      };

      const promise = service.updateShare('share-001', 'specific', ['new@test.com']);

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/shares/share-001`);
        expect(req.request.body).toEqual({
          accessLevel: 'specific',
          allowedEmails: ['new@test.com'],
        });
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.allowedEmails).toContain('new@test.com');
    });
  });

  // -----------------------------------------------------------------------
  // exportSharedConversation
  // -----------------------------------------------------------------------

  describe('exportSharedConversation', () => {
    it('should POST /shares/{shareId}/export', async () => {
      const mockResponse: ExportResponse = {
        sessionId: 'new-sess-001',
        title: 'Test Conversation (shared)',
      };

      const promise = service.exportSharedConversation('share-001');

      await vi.waitFor(() => {
        const req = httpMock.expectOne(`${baseUrl}/shares/share-001/export`);
        expect(req.request.method).toBe('POST');
        expect(req.request.body).toEqual({});
        req.flush(mockResponse);
      });

      const result = await promise;
      expect(result.sessionId).toBe('new-sess-001');
      expect(result.title).toBe('Test Conversation (shared)');
    });

    it('should call ensureAuthenticated before export', async () => {
      const authService = TestBed.inject(AuthService);
      const mockResponse: ExportResponse = {
        sessionId: 'new-sess-001',
        title: 'Test (shared)',
      };

      const promise = service.exportSharedConversation('share-001');

      await vi.waitFor(() => {
        httpMock.expectOne(`${baseUrl}/shares/share-001/export`).flush(mockResponse);
      });

      await promise;
      expect(authService.ensureAuthenticated).toHaveBeenCalled();
    });
  });
});
