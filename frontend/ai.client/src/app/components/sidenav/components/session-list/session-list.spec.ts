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
      sessionsResource: { value: vi.fn().mockReturnValue(null), error: vi.fn().mockReturnValue(null), isPending: vi.fn().mockReturnValue(false) },
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

  it('should compute sessions from merged resource', async () => {
    const component = await createComponent();
    expect(component.sessions()).toEqual([mockSession]);
  });

  it('should return title or fallback for untitled sessions', async () => {
    const component = await createComponent();
    expect(component['getSessionTitle'](mockSession)).toBe('Test Session');
    expect(component['getSessionTitle']({ ...mockSession, title: '' })).toBe('Untitled Session');
  });

  it('should close sidenav on session click', async () => {
    const component = await createComponent();
    component['onSessionClick']();
    expect(mockSidenavService.close).toHaveBeenCalled();
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
});
