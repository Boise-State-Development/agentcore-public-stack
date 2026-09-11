import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideLocationMocks } from '@angular/common/testing';
import { Router, provideRouter } from '@angular/router';
import { Component, input, output, signal } from '@angular/core';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { AgentService } from '../../agents/services/agent.service';

describe('Sidenav', () => {
  let mockRouter: any;
  let mockSessionService: any;
  let mockBffSession: any;
  let mockSidenavService: any;
  let mockUserService: any;

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
      canAccessAdmin: signal(false),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: mockRouter },
        { provide: SessionService, useValue: mockSessionService },
        { provide: BffSessionService, useValue: mockBffSession },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: UserService, useValue: mockUserService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function createComponent() {
    const { Sidenav } = await import('./sidenav');
    const component = TestBed.runInInjectionContext(() => new Sidenav());
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

});

/**
 * The nav entries, rendered from the real template.
 *
 * Both Agents and Artifacts are **unconditional**: nothing about them waits on a feature
 * probe, a role, or a network round-trip. That is what these assert, and it is a
 * regression guard in two directions — an entry that reappears behind an `@if`, and the
 * boot-time list fetch that `@if` used to ride.
 *
 * They render the real template rather than reading a computed, because the bugs they
 * guard against live *only* there: `showAgents()` was already true for every user while
 * `@if (showAgents() && isAdmin())` hid the entry anyway.
 *
 * The child components are stubbed — pulling `SessionList` / `UserDropdownComponent` in
 * would drag their dependency graphs with them.
 */
describe('Sidenav — nav entries', () => {
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
      // The sidenav's admin entry point moved to `canAccessAdmin` so delegated
      // admins (no system_admin role, but some admin scope) still get the link.
      canAccessAdmin: signal(false),
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
        // Still provided, though the component no longer injects it: that is what
        // makes the "fetches nothing at boot" assertion below a real guard.
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

  it('renders the Agents nav entry for a NON-admin', async () => {
    mockUserService.isAdmin.set(false);
    mockUserService.canAccessAdmin.set(false);
    const fixture = await renderSidenav();

    // Fails against the pre-GA `@if (showAgents() && isAdmin())`.
    expect(agentsNavLink(fixture)).toBeDefined();
    expect(agentsNavLink(fixture)!.textContent).toContain('Agents');
  });

  it('renders the Agents nav entry for an admin', async () => {
    mockUserService.isAdmin.set(true);
    mockUserService.canAccessAdmin.set(true);
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)).toBeDefined();
  });

  it('renders Agents on first paint, without waiting on the agent list', async () => {
    // The entry used to hang on `showAgents()` — "the /agents list call did not 404" —
    // so it could only appear a round-trip after the nav around it, popping into a
    // sidebar the user was already reading. `accessible$` unresolved is exactly that
    // pre-response state, and the entry must already be there.
    mockAgentService.accessible$.set(null);
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)).toBeDefined();
  });

  it('renders Agents even when the agent surface 404s', async () => {
    // The trade made when the gate came off: with `AGENTS_API_ENABLED` off the entry
    // leads to an empty agents page (which swallows the error itself) rather than
    // being absent. Asserted so the layout shift is not quietly reintroduced.
    mockAgentService.accessible$.set(false);
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)).toBeDefined();
  });

  it('fetches nothing at boot — the nav no longer probes feature accessibility', async () => {
    // Every real consumer of the agent list (the agents page, the composer `@`-menu,
    // the schedule form) loads it itself. The sidenav's copy existed only to feed the
    // gate above, so rendering the nav must cost no HTTP at all.
    await renderSidenav();
    expect(mockAgentService.loadAgents).not.toHaveBeenCalled();
  });

  // ── Assistant deprecation, finished ───────────────────────────────────────────────
  //
  // The Assistants signpost is gone: the rename has landed with users, so the old noun no
  // longer needs a door in the nav. `/assistants` still resolves to the migration
  // explainer for anyone holding a link — this only asserts the nav does not offer it,
  // and in particular that nothing here leads to a second authoring surface.
  it('no longer signposts Assistants anywhere in the nav', async () => {
    const fixture = await renderSidenav();
    const html = fixture.nativeElement as HTMLElement;

    expect(html.querySelector('a[href="/assistants"]')).toBeNull();
    expect(html.querySelector('a[href^="/assistants/"]')).toBeNull();
    expect(html.textContent).not.toContain('Assistants');
  });

  it('drops the "New" badge on Agents — the rename has stopped being news', async () => {
    const fixture = await renderSidenav();
    expect(agentsNavLink(fixture)!.textContent).not.toContain('New');
    expect(agentsNavLink(fixture)!.textContent).not.toContain('Preview');
  });

  // ── Artifacts ─────────────────────────────────────────────────────────────────────
  //
  // `/artifacts` carries only `authGuard`, so there is no kill switch for the entry to
  // ride even in principle: it is there for every signed-in user.
  function artifactsNavLink(fixture: ComponentFixture<unknown>): HTMLAnchorElement | null {
    return fixture.nativeElement.querySelector('a[href="/artifacts"]');
  }

  it('offers the Artifacts library', async () => {
    const fixture = await renderSidenav();

    expect(artifactsNavLink(fixture)).not.toBeNull();
    expect(artifactsNavLink(fixture)!.textContent).toContain('Artifacts');
  });

  it('renders Artifacts on first paint too', async () => {
    mockAgentService.accessible$.set(null);
    const fixture = await renderSidenav();
    expect(artifactsNavLink(fixture)).not.toBeNull();
  });
});
