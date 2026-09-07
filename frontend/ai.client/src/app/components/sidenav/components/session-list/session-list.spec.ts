import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { SessionService } from '../../../../session/services/session/session.service';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';

describe('SessionList', () => {
  let mockSessionService: any;
  let mockSidenavService: any;
  let mockToastService: any;
  let mockDialog: any;
  let mockRouter: any;

  const mockSession = {
    sessionId: 'test-session',
    userId: 'user-1',
    title: 'Test Session',
    status: 'active' as const,
    createdAt: '2024-01-01T00:00:00Z',
    lastMessageAt: '2024-01-01T00:00:00Z',
    messageCount: 5,
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockSessionService = {
      mergedSessionsResource: signal({ sessions: [mockSession], nextToken: null }),
      currentSession: signal(mockSession),
      deleteSession: vi.fn().mockResolvedValue(undefined),
      sessionsResource: { value: vi.fn().mockReturnValue({ sessions: [mockSession], nextToken: null }), error: vi.fn().mockReturnValue(null), isPending: vi.fn().mockReturnValue(false) },
      isLocallyRead: vi.fn().mockReturnValue(false),
      markSessionRead: vi.fn().mockResolvedValue(undefined),
      markSessionUnread: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn(),
    };
    mockSidenavService = { close: vi.fn() };
    mockToastService = { success: vi.fn(), error: vi.fn() };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of(true) }) };
    mockRouter = { navigate: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: mockSessionService },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: ToastService, useValue: mockToastService },
        { provide: Dialog, useValue: mockDialog },
        { provide: Router, useValue: mockRouter },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { SessionList } = await import('./session-list');
    return TestBed.runInInjectionContext(() => new SessionList());
  }

  describe('isLoading', () => {
    it('stays loading while the resource has not produced a response', async () => {
      // Cold start: the loader short-circuits to `null` because sessions
      // loading is not enabled until the BFF bootstrap resolves — and
      // `reload()` keeps that `null` for the whole real fetch. With nothing
      // cached to draw, that must read as loading (skeleton), never as
      // "no conversations".
      mockSessionService.mergedSessionsResource.set({ sessions: [], nextToken: null });
      mockSessionService.sessionsResource.value.mockReturnValue(null);
      const component = await createComponent();

      expect(component.isLoading()).toBe(true);

      // Before the first load resolves at all.
      mockSessionService.sessionsResource.value.mockReturnValue(undefined);
      expect(component.isLoading()).toBe(true);
    });

    it('is not loading once the API answers, even with zero sessions', async () => {
      mockSessionService.mergedSessionsResource.set({ sessions: [], nextToken: null });
      mockSessionService.sessionsResource.value.mockReturnValue({ sessions: [], nextToken: null });
      const component = await createComponent();

      // A real empty response is the empty state, not a skeleton.
      expect(component.isLoading()).toBe(false);
    });

    it('renders locally cached sessions instead of a skeleton', async () => {
      mockSessionService.sessionsResource.value.mockReturnValue(null);
      const component = await createComponent();

      expect(component.isLoading()).toBe(false);
    });

    it('defers to the error state without reading the resource value', async () => {
      mockSessionService.mergedSessionsResource.set({ sessions: [], nextToken: null });
      mockSessionService.sessionsResource.error.mockReturnValue(new Error('boom'));
      // Angular's resource throws from `value()` when the load errored.
      mockSessionService.sessionsResource.value.mockImplementation(() => {
        throw new Error('should not be read');
      });
      const component = await createComponent();

      expect(component.isLoading()).toBe(false);
    });
  });

  it('should compute sessions from merged resource', async () => {
    const component = await createComponent();
    expect(component.sessions()).toEqual([mockSession]);
  });

  it('should return title or fallback for untitled sessions', async () => {
    const component = await createComponent();
    expect(component['getSessionTitle'](mockSession)).toBe('Test Session');
    expect(component['getSessionTitle']({ ...mockSession, title: '' })).toBe('Untitled Session');
  });

  it('closes the sidenav and optimistically sets the clicked session on click', async () => {
    const component = await createComponent();
    mockSessionService.currentSession.set({ ...mockSession, sessionId: 'other', title: 'Other' });

    component['onSessionClick'](mockSession);

    expect(mockSidenavService.close).toHaveBeenCalled();
    expect(mockSessionService.currentSession()).toEqual(mockSession);
  });

  it('reflects per-session streaming state for the in-progress indicator', async () => {
    const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
    const chatState = TestBed.inject(ChatStateService);
    const component = await createComponent();

    expect(component['isSessionStreaming']('test-session')).toBe(false);

    chatState.setChatLoading('test-session', true);
    expect(component['isSessionStreaming']('test-session')).toBe(true);
    // Only the streaming conversation shows the indicator.
    expect(component['isSessionStreaming']('other-session')).toBe(false);

    chatState.setChatLoading('test-session', false);
    expect(component['isSessionStreaming']('test-session')).toBe(false);
  });

  it('shows the unread dot for a server-unread session and suppresses it once locally read', async () => {
    const component = await createComponent();
    const unreadSession = { ...mockSession, unread: true };

    // Server flag set, not yet locally read → dot shows.
    expect(component['shouldShowUnreadDot'](unreadSession)).toBe(true);

    // User opened it: local read-watermark suppresses the dot before the
    // server round-trips.
    mockSessionService.isLocallyRead.mockReturnValue(true);
    expect(component['shouldShowUnreadDot'](unreadSession)).toBe(false);

    // A session with no server flag and no client signal shows nothing.
    mockSessionService.isLocallyRead.mockReturnValue(false);
    expect(component['shouldShowUnreadDot'](mockSession)).toBe(false);
  });

  it('ORs the client-side interactive unread signal into the dot', async () => {
    const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
    const chatState = TestBed.inject(ChatStateService);
    const component = await createComponent();

    // No server flag, but a stream finished in this tab while viewing elsewhere.
    chatState.setViewedSession('other');
    chatState.setChatLoading('test-session', true);
    chatState.setChatLoading('test-session', false);

    expect(component['shouldShowUnreadDot'](mockSession)).toBe(true);
  });

  it('marks a server-unread session read on open, but not a read one', async () => {
    const component = await createComponent();

    component['onSessionClick'](mockSession);
    expect(mockSessionService.markSessionRead).not.toHaveBeenCalled();

    component['onSessionClick']({ ...mockSession, unread: true });
    expect(mockSessionService.markSessionRead).toHaveBeenCalledOnce();
  });

  it('marks a read session unread from the options menu and shows the dot optimistically', async () => {
    const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
    const chatState = TestBed.inject(ChatStateService);
    const markSpy = vi.spyOn(chatState, 'markSessionUnread');
    const component = await createComponent();

    // mockSession has no dot → the toggle marks it unread.
    component['onToggleReadClick'](new Event('click'), mockSession);
    // The mutation is deferred past the menu close (queueMicrotask).
    await Promise.resolve();

    expect(mockSessionService.markSessionUnread).toHaveBeenCalledWith(mockSession);
    // Optimistic client-side flag surfaces the dot without waiting on the refetch.
    expect(markSpy).toHaveBeenCalledWith(mockSession.sessionId);
    expect(component['shouldShowUnreadDot'](mockSession)).toBe(true);
    // markSessionUnread only refetches after its POST resolves, so the row is
    // kicked to re-render synchronously — mirroring the mark-read branch.
    expect(mockSessionService.refreshSessions).toHaveBeenCalled();
    expect(mockSessionService.markSessionRead).not.toHaveBeenCalled();
  });

  it('marks an unread session read and clears the client-side dot', async () => {
    const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
    const chatState = TestBed.inject(ChatStateService);
    const clearSpy = vi.spyOn(chatState, 'clearSessionUnread');
    const component = await createComponent();
    const unreadSession = { ...mockSession, unread: true }; // server dot shows

    component['onToggleReadClick'](new Event('click'), unreadSession);
    await Promise.resolve();

    expect(mockSessionService.markSessionRead).toHaveBeenCalledWith(unreadSession);
    expect(clearSpy).toHaveBeenCalledWith(unreadSession.sessionId);
    // markSessionRead skips the refetch, so the list is kicked to re-render.
    expect(mockSessionService.refreshSessions).toHaveBeenCalled();
    expect(mockSessionService.markSessionUnread).not.toHaveBeenCalled();
  });

  it('marks the title pending only for a titleless session that is streaming', async () => {
    const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
    const chatState = TestBed.inject(ChatStateService);
    const component = await createComponent();
    const untitled = { ...mockSession, sessionId: 'test-session', title: '' };

    // Titleless but not streaming yet → static fallback, no shimmer.
    expect(component['isTitlePending'](untitled)).toBe(false);

    // Streaming its first response with no title yet → shimmer.
    chatState.setChatLoading('test-session', true);
    expect(component['isTitlePending'](untitled)).toBe(true);

    // Title landed mid-stream → shimmer clears even while still streaming.
    expect(component['isTitlePending']({ ...untitled, title: 'Generated' })).toBe(false);

    // Stream ends without a title → shimmer clears; row shows the fallback.
    chatState.setChatLoading('test-session', false);
    expect(component['isTitlePending'](untitled)).toBe(false);
  });
});
