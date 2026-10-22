import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { SessionService } from '../../../../session/services/session/session.service';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';
import { ProjectsService } from '../../../../projects/services/projects.service';
import { FEATURES } from '../../../../services/features';

describe('SessionList', () => {
  let mockSessionService: any;
  let mockSidenavService: any;
  let mockToastService: any;
  let mockDialog: any;
  let mockRouter: any;
  let mockProjectsService: any;

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
      sessionsResource: { value: vi.fn().mockReturnValue({ sessions: [mockSession], nextToken: null }), error: vi.fn().mockReturnValue(null), isPending: vi.fn().mockReturnValue(false), isLoading: signal(false) },
      isLoadingMoreSessions: signal(false),
      loadMoreSessionsError: signal(false),
      loadMoreSessions: vi.fn().mockResolvedValue(undefined),
      isLocallyRead: vi.fn().mockReturnValue(false),
      markSessionRead: vi.fn().mockResolvedValue(undefined),
      markSessionUnread: vi.fn().mockResolvedValue(undefined),
      refreshSessions: vi.fn(),
      pinnedSessions: signal([]),
      pinnedSessionIds: signal(new Set<string>()),
      setSessionPinned: vi.fn().mockResolvedValue(undefined),
    };
    mockSidenavService = { close: vi.fn() };
    mockToastService = { success: vi.fn(), error: vi.fn() };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of(true) }) };
    mockRouter = { navigate: vi.fn() };
    mockProjectsService = {
      projects$: signal([{ projectId: 'prj_1', name: 'Enrollment Sync' }]),
      available$: signal<boolean | null>(null),
      loading$: signal(false),
      load: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: mockSessionService },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: ToastService, useValue: mockToastService },
        { provide: Dialog, useValue: mockDialog },
        { provide: Router, useValue: mockRouter },
        { provide: ProjectsService, useValue: mockProjectsService },
        { provide: FEATURES, useValue: { projects: true } },
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

  describe('loading more as the end of the list comes into view', () => {
    beforeEach(() => {
      mockSessionService.mergedSessionsResource.set({ sessions: [mockSession], nextToken: 'p2' });
    });

    it('fetches one page per sighting, then asks the sentinel to re-measure', async () => {
      const component = await createComponent();
      component['endOfListVisible'].set(true);
      TestBed.tick();

      expect(mockSessionService.loadMoreSessions).toHaveBeenCalledTimes(1);
      // The sighting is spent — a stale "visible" must not buy a second page.
      expect(component['endOfListVisible']()).toBe(false);
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).toHaveBeenCalledTimes(1);

      await vi.waitFor(() => expect(component['endOfListRemeasure']()).toBe(1));
    });

    it('does nothing until the sentinel is in view', async () => {
      await createComponent();
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).not.toHaveBeenCalled();
    });

    it('does nothing once the list is exhausted', async () => {
      mockSessionService.mergedSessionsResource.set({ sessions: [mockSession], nextToken: null });
      const component = await createComponent();
      component['endOfListVisible'].set(true);
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).not.toHaveBeenCalled();
    });

    it('holds a sighting that arrives mid-reload until the reload settles', async () => {
      mockSessionService.sessionsResource.isLoading.set(true);
      const component = await createComponent();
      component['endOfListVisible'].set(true);
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).not.toHaveBeenCalled();
      expect(component['endOfListVisible']()).toBe(true);

      mockSessionService.sessionsResource.isLoading.set(false);
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).toHaveBeenCalledTimes(1);
    });

    it('stops after a failed page until the user retries', async () => {
      mockSessionService.loadMoreSessionsError.set(true);
      const component = await createComponent();
      component['endOfListVisible'].set(true);
      TestBed.tick();
      expect(mockSessionService.loadMoreSessions).not.toHaveBeenCalled();

      component['retryLoadMore']();
      expect(mockSessionService.loadMoreSessions).toHaveBeenCalledTimes(1);
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

  describe('row indicator precedence', () => {
    const pausedUnread = { ...mockSession, unread: true, awaitingInput: 'oauth' as const };

    it('ranks streaming over a paused turn over unread', async () => {
      const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
      const chatState = TestBed.inject(ChatStateService);
      const component = await createComponent();

      expect(component['rowIndicator'](pausedUnread)).toBe('awaiting-input');
      expect(component['rowIndicator']({ ...mockSession, unread: true })).toBe('unread');
      expect(component['rowIndicator'](mockSession)).toBeNull();

      chatState.setChatLoading('test-session', true);
      expect(component['rowIndicator'](pausedUnread)).toBe('streaming');
    });

    it('shows the paused state from a live stream before the list knows', async () => {
      const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
      const chatState = TestBed.inject(ChatStateService);
      const component = await createComponent();

      // A background conversation's stream ends on an approval prompt.
      chatState.setViewedSession('other');
      chatState.setChatLoading('test-session', true);
      chatState.markSessionAwaitingInput('test-session', 'tool_approval');
      chatState.setChatLoading('test-session', false);

      // It is also unread now, and the paused state outranks that.
      expect(component['shouldShowUnreadDot'](mockSession)).toBe(true);
      expect(component['rowIndicator'](mockSession)).toBe('awaiting-input');
    });

    it('drops a stale server pause once this tab saw the turn resume', async () => {
      const { ChatStateService } = await import('../../../../session/services/chat/chat-state.service');
      const chatState = TestBed.inject(ChatStateService);
      const component = await createComponent();

      chatState.setViewedSession('test-session');
      expect(component['rowIndicator'](pausedUnread)).toBe('awaiting-input');

      // Answering resumes the turn on a new stream; the list row is not
      // refetched yet and still says oauth.
      chatState.setChatLoading('test-session', true);
      chatState.setChatLoading('test-session', false);
      expect(component['isAwaitingInput'](pausedUnread)).toBe(false);
    });
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

  describe('project grouping', () => {
    const now = new Date().toISOString();
    const plain = (id: string) => ({ ...mockSession, sessionId: id, lastMessageAt: now, createdAt: now });
    const task = (id: string, projectId: string) => ({ ...plain(id), preferences: { assistantId: 'ast', projectId } });

    it('groups project tasks under their project inside the time bucket, in recency order', async () => {
      mockSessionService.mergedSessionsResource.set({
        sessions: [plain('a'), task('b', 'prj_1'), plain('c'), task('d', 'prj_1'), task('e', 'prj_gone')],
        nextToken: null,
      });
      const component = await createComponent();
      const [today] = component.groupedSessions();
      expect(today.label).toBe('Today');
      // Buckets keep every session; grouping only reshapes the rows.
      expect(today.sessions.map((s: any) => s.sessionId)).toEqual(['a', 'b', 'c', 'd', 'e']);
      expect(today.entries.map((e: any) => (e.kind === 'session' ? e.session.sessionId : `${e.name}:${e.sessions.map((s: any) => s.sessionId).join(',')}`))).toEqual([
        'a',
        'Enrollment Sync:b,d',
        'c',
        // Not in the caller's project list (left, or not loaded yet): a generic heading.
        'Project:e',
      ]);
    });

    it('opens a project task as `?project=`, and any other Agent conversation by its Agent', async () => {
      const component = await createComponent();
      expect(component['getSessionQueryParams'](task('b', 'prj_1'))).toEqual({ project: 'prj_1' });
      expect(component['getSessionQueryParams']({ ...plain('c'), preferences: { assistantId: 'agent-7' } })).toEqual({ assistantId: 'agent-7' });
      expect(component['getSessionQueryParams'](plain('a'))).toBeNull();
    });

    it('opens a project task by its Agent in a build with Projects off', async () => {
      TestBed.overrideProvider(FEATURES, { useValue: { projects: false } });
      const component = await createComponent();
      expect(component['getSessionQueryParams'](task('b', 'prj_1'))).toEqual({ assistantId: 'ast' });
    });

    it('lists project tasks as plain rows, loads no names, and offers no project share in a build with Projects off', async () => {
      TestBed.overrideProvider(FEATURES, { useValue: { projects: false } });
      mockSessionService.mergedSessionsResource.set({ sessions: [plain('a'), task('b', 'prj_1')], nextToken: null });
      const component = await createComponent();
      TestBed.tick();
      const [today] = component.groupedSessions();
      expect(today.entries.map((e: any) => e.kind)).toEqual(['session', 'session']);
      expect(mockProjectsService.load).not.toHaveBeenCalled();
      const event = { preventDefault: vi.fn(), stopPropagation: vi.fn() } as unknown as Event;
      (component as any).onShareClick(event, task('b', 'prj_1'));
      expect(mockDialog.open.mock.calls.at(-1)[1].data.projectId).toBeNull();
    });

    it('loads project names once, and only for someone with a project task', async () => {
      await createComponent();
      TestBed.tick();
      expect(mockProjectsService.load).not.toHaveBeenCalled();

      mockSessionService.mergedSessionsResource.set({ sessions: [task('b', 'prj_1')], nextToken: null });
      TestBed.tick();
      mockSessionService.mergedSessionsResource.set({ sessions: [task('b', 'prj_1'), task('x', 'prj_1')], nextToken: null });
      TestBed.tick();
      expect(mockProjectsService.load).toHaveBeenCalledTimes(1);
    });

    it('does not reload a project list that is already loaded', async () => {
      mockProjectsService.available$.set(true);
      mockSessionService.mergedSessionsResource.set({ sessions: [task('b', 'prj_1')], nextToken: null });
      await createComponent();
      TestBed.tick();
      expect(mockProjectsService.load).not.toHaveBeenCalled();
    });

    it('passes the project to the share modal', async () => {
      const component = await createComponent();
      const event = { preventDefault: vi.fn(), stopPropagation: vi.fn() } as unknown as Event;
      (component as any).onShareClick(event, task('b', 'prj_1'));
      expect(mockDialog.open.mock.calls.at(-1)[1].data.projectId).toBe('prj_1');
      (component as any).onShareClick(event, plain('a'));
      expect(mockDialog.open.mock.calls.at(-1)[1].data.projectId).toBeNull();
    });
  });
});
