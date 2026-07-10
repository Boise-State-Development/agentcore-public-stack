import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { signal } from '@angular/core';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { MemorySpaceService } from '../../memory-spaces/services/memory-space.service';
import { AgentService } from '../../agents/services/agent.service';

describe('Sidenav', () => {
  let mockRouter: any;
  let mockSessionService: any;
  let mockBffSession: any;
  let mockSidenavService: any;
  let mockUserService: any;
  let mockMemorySpaceService: any;
  let mockAgentService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockRouter = { navigate: vi.fn() };
    mockSessionService = {
      currentSession: signal({ sessionId: 'test-session', userId: 'u1', title: 'Test Session', status: 'active' as const, createdAt: '', lastMessageAt: '', messageCount: 0 }),
      hasCurrentSession: signal(true),
    };
    // Phase 6c: logout is owned by the BFF SessionService now.
    mockBffSession = { logout: vi.fn().mockResolvedValue(undefined) };
    mockSidenavService = {
      isCollapsed: signal(false),
      close: vi.fn(),
      toggleCollapsed: vi.fn(),
    };
    mockUserService = {
      hasAnyRole: vi.fn().mockReturnValue(false),
      currentUser: signal(null),
      isAdmin: signal(false),
    };
    mockMemorySpaceService = {
      accessible$: signal<boolean | null>(null),
      loadSpaces: vi.fn().mockResolvedValue(undefined),
    };
    mockAgentService = {
      accessible$: signal<boolean | null>(null),
      loadAgents: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: mockRouter },
        { provide: SessionService, useValue: mockSessionService },
        { provide: BffSessionService, useValue: mockBffSession },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: UserService, useValue: mockUserService },
        { provide: MemorySpaceService, useValue: mockMemorySpaceService },
        { provide: AgentService, useValue: mockAgentService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { Sidenav } = await import('./sidenav');
    const component = TestBed.runInInjectionContext(() => new Sidenav());
    TestBed.tick(); // flush the constructor effect that probes feature accessibility
    return component;
  }

  it('should compute current session title', async () => {
    const component = await createComponent();
    expect(component.currentSessionTitle()).toBe('Test Session');

    mockSessionService.currentSession.set({ ...mockSessionService.currentSession(), title: '' });
    expect(component.currentSessionTitle()).toBe('Untitled Session');
  });

  it('should start new session and close sidenav', async () => {
    const component = await createComponent();
    component.newSession();
    expect(mockSidenavService.close).toHaveBeenCalled();
    expect(mockRouter.navigate).toHaveBeenCalledWith(['']);
  });

  it('should toggle sidenav collapse', async () => {
    const component = await createComponent();
    component.toggleCollapse();
    expect(mockSidenavService.toggleCollapsed).toHaveBeenCalled();
  });

  it('should handle logout via the BFF and route the user to /auth/login', async () => {
    const component = await createComponent();
    await component.handleLogout();
    expect(mockBffSession.logout).toHaveBeenCalledTimes(1);
    expect(mockRouter.navigate).toHaveBeenCalledWith(['/auth/login']);
  });

  it('probes memory-space accessibility once a user is authenticated', async () => {
    mockUserService.currentUser.set({ user_id: 'u1', email: 'u1@example.com' });
    await createComponent();
    expect(mockMemorySpaceService.loadSpaces).toHaveBeenCalled();
  });

  it('does not probe memory-space accessibility while unauthenticated', async () => {
    mockUserService.currentUser.set(null);
    await createComponent();
    expect(mockMemorySpaceService.loadSpaces).not.toHaveBeenCalled();
  });

  it('hides the Memory Spaces nav entry until accessibility resolves true', async () => {
    const component = await createComponent();

    mockMemorySpaceService.accessible$.set(null);
    expect(component.showMemorySpaces()).toBe(false);

    mockMemorySpaceService.accessible$.set(false);
    expect(component.showMemorySpaces()).toBe(false);

    mockMemorySpaceService.accessible$.set(true);
    expect(component.showMemorySpaces()).toBe(true);
  });

  it('probes agent accessibility once a user is authenticated', async () => {
    mockUserService.currentUser.set({ user_id: 'u1', email: 'u1@example.com' });
    await createComponent();
    expect(mockAgentService.loadAgents).toHaveBeenCalled();
  });

  it('does not probe agent accessibility while unauthenticated', async () => {
    mockUserService.currentUser.set(null);
    await createComponent();
    expect(mockAgentService.loadAgents).not.toHaveBeenCalled();
  });

  it('hides the Agents nav entry until accessibility resolves true', async () => {
    const component = await createComponent();

    mockAgentService.accessible$.set(null);
    expect(component.showAgents()).toBe(false);

    mockAgentService.accessible$.set(false);
    expect(component.showAgents()).toBe(false);

    mockAgentService.accessible$.set(true);
    expect(component.showAgents()).toBe(true);
  });
});
