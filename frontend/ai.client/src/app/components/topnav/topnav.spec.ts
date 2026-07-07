import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { signal } from '@angular/core';
import { SessionService } from '../../session/services/session/session.service';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';

describe('Topnav', () => {
  let mockRouter: any;
  let mockSessionService: any;
  let mockSidenavService: any;
  let mockChatStateService: any;
  let metadataLoading: ReturnType<typeof signal<boolean>>;
  let loadingSessionIds: Set<string>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockRouter = { navigate: vi.fn() };
    metadataLoading = signal(false);
    mockSessionService = {
      currentSession: signal({ sessionId: 'test-session', title: 'Test Session' }),
      sessionMetadataResource: { isLoading: metadataLoading },
      markSessionRead: vi.fn(),
      markSessionUnread: vi.fn(),
      refreshSessions: vi.fn(),
    };
    loadingSessionIds = new Set<string>();
    mockChatStateService = {
      isSessionLoading: (id: string) => loadingSessionIds.has(id),
      markSessionUnread: vi.fn(),
      clearSessionUnread: vi.fn(),
    };
    mockSidenavService = { open: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: mockRouter },
        { provide: SessionService, useValue: mockSessionService },
        { provide: ChatStateService, useValue: mockChatStateService },
        { provide: SidenavService, useValue: mockSidenavService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { Topnav } = await import('./topnav');
    return TestBed.runInInjectionContext(() => new Topnav());
  }

  it('should expose currentSession from SessionService', async () => {
    const component = await createComponent();
    expect(component.currentSession()).toEqual({ sessionId: 'test-session', title: 'Test Session' });
  });

  it('should navigate to home when newChat is called', async () => {
    const component = await createComponent();
    component.newChat();
    expect(mockRouter.navigate).toHaveBeenCalledWith(['']);
  });

  it('should open sidenav when openSidenav is called', async () => {
    const component = await createComponent();
    component.openSidenav();
    expect(mockSidenavService.open).toHaveBeenCalled();
  });

  describe('title pending / fallback', () => {
    it('is not pending when the session already has a title', async () => {
      const component = await createComponent();
      expect((component as any).titlePending()).toBe(false);
      expect((component as any).displayTitle()).toBe('Test Session');
    });

    it('is pending while metadata is still loading (hard refresh)', async () => {
      mockSessionService.currentSession.set({ sessionId: 'sess-1', title: '' });
      metadataLoading.set(true);
      const component = await createComponent();
      expect((component as any).titlePending()).toBe(true);
    });

    it('is pending while a brand-new session is streaming its first response', async () => {
      mockSessionService.currentSession.set({ sessionId: 'sess-1', title: '' });
      loadingSessionIds.add('sess-1');
      const component = await createComponent();
      expect((component as any).titlePending()).toBe(true);
    });

    it('falls back to "Untitled Session" once resolved with no title', async () => {
      // Not loading, not streaming, empty title → the skeleton must clear.
      mockSessionService.currentSession.set({ sessionId: 'sess-1', title: '' });
      const component = await createComponent();
      expect((component as any).titlePending()).toBe(false);
      expect((component as any).displayTitle()).toBe('Untitled Session');
    });
  });

  describe('mark as read/unread toggle', () => {
    it('marks a read session unread and surfaces the sidebar dot optimistically', async () => {
      mockSessionService.currentSession.set({ sessionId: 'sess-1', title: 'T', unread: false });
      const component = await createComponent();

      expect((component as any).isCurrentUnread()).toBe(false);
      (component as any).onToggleReadClick(new Event('click'));

      expect(mockSessionService.markSessionUnread).toHaveBeenCalled();
      expect(mockChatStateService.markSessionUnread).toHaveBeenCalledWith('sess-1');
      expect(mockSessionService.markSessionRead).not.toHaveBeenCalled();
    });

    it('marks an unread session read, clears the client dot, and refreshes the sidebar', async () => {
      mockSessionService.currentSession.set({ sessionId: 'sess-1', title: 'T', unread: true });
      const component = await createComponent();

      expect((component as any).isCurrentUnread()).toBe(true);
      (component as any).onToggleReadClick(new Event('click'));

      expect(mockSessionService.markSessionRead).toHaveBeenCalled();
      expect(mockChatStateService.clearSessionUnread).toHaveBeenCalledWith('sess-1');
      expect(mockSessionService.refreshSessions).toHaveBeenCalled();
      expect(mockSessionService.markSessionUnread).not.toHaveBeenCalled();
    });
  });
});
