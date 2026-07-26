import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideLocationMocks } from '@angular/common/testing';
import { Router, provideRouter } from '@angular/router';
import { Component, input, output, signal } from '@angular/core';
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

/**
 * Template-level gating (marketplace D14).
 *
 * These render the real template rather than reading the computed, because the bug they
 * guard against lives *only* in the template: `showAgents()` was already true for every
 * user while `@if (showAgents() && isAdmin())` hid the entry anyway. A spec that asserts
 * the computed passes against both the gated and the GA'd code, so it proves nothing.
 *
 * The child components are stubbed — this is a spec about one `@if`, and pulling
 * `SessionList` / `UserDropdownComponent` in would drag their dependency graphs with them.
 */
describe('Sidenav — Agents nav entry gating (D14)', () => {
  @Component({ selector: 'app-session-list', template: '' })
  class SessionListStub {}

  @Component({ selector: 'app-user-dropdown', template: '' })
  class UserDropdownStub {
    readonly user = input<unknown>();
    readonly isAdmin = input<boolean>(false);
    readonly logout = output<void>();
  }

  let mockUserService: any;
  let mockAgentService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockUserService = {
      hasAnyRole: vi.fn().mockReturnValue(false),
      currentUser: signal({ user_id: 'u1', email: 'u1@example.com' }),
      isAdmin: signal(false),
    };
    mockAgentService = {
      accessible$: signal<boolean | null>(true),
      loadAgents: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideLocationMocks(),
        {
          provide: SessionService,
          useValue: {
            currentSession: signal({ sessionId: 's1', userId: 'u1', title: 'T', status: 'active' as const, createdAt: '', lastMessageAt: '', messageCount: 0 }),
            hasCurrentSession: signal(true),
          },
        },
        { provide: BffSessionService, useValue: { logout: vi.fn() } },
        {
          provide: SidenavService,
          useValue: { isCollapsed: signal(false), close: vi.fn(), toggleCollapsed: vi.fn() },
        },
        { provide: UserService, useValue: mockUserService },
        {
          provide: MemorySpaceService,
          useValue: { accessible$: signal<boolean | null>(false), loadSpaces: vi.fn().mockResolvedValue(undefined) },
        },
        { provide: AgentService, useValue: mockAgentService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function renderSidenav() {
    const { Sidenav } = await import('./sidenav');
    const { SessionList } = await import('./components/session-list/session-list');
    const { UserDropdownComponent } = await import('../topnav/components/user-dropdown.component');

    TestBed.overrideComponent(Sidenav, {
      remove: { imports: [SessionList, UserDropdownComponent] },
      add: { imports: [SessionListStub, UserDropdownStub] },
    });

    const fixture = TestBed.createComponent(Sidenav);
    fixture.detectChanges();
    return fixture;
  }

  function agentsNavLink(fixture: ComponentFixture<unknown>): HTMLAnchorElement | undefined {
    const anchors = fixture.nativeElement.querySelectorAll('a[href="/agents"]');
    return anchors.length ? (anchors[0] as HTMLAnchorElement) : undefined;
  }

  it('renders the Agents nav entry for a NON-admin once the surface is reachable', async () => {
    mockUserService.isAdmin.set(false);
    const fixture = await renderSidenav();

    // Fails against the pre-GA `@if (showAgents() && isAdmin())`.
    expect(agentsNavLink(fixture)).toBeDefined();
    expect(agentsNavLink(fixture)!.textContent).toContain('Agents');
  });

  it('renders the Agents nav entry for an admin', async () => {
    mockUserService.isAdmin.set(true);
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)).toBeDefined();
  });

  it('still hides the Agents nav entry when the surface 404s, regardless of role', async () => {
    mockAgentService.accessible$.set(false);
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)).toBeUndefined();
  });
});
