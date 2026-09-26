import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideLocationMocks } from '@angular/common/testing';
import { Router, provideRouter } from '@angular/router';
import { Component, input, output, signal } from '@angular/core';
import { Subject } from 'rxjs';
import { ADMIN_CHROME } from '../../shared/utils/route-chrome';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { AgentService } from '../../agents/services/agent.service';
import { FEATURES } from '../../services/features';

describe('Sidenav', () => {
  let mockRouter: any;
  let mockSessionService: any;
  let mockBffSession: any;
  let mockSidenavService: any;
  let mockUserService: any;
  let routerEvents!: Subject<unknown>;
  let routerRoot: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    // `events` and `routerState` are not optional extras on this double:
    // the sidenav derives which navigation to show (chat vs the admin
    // console) from the active route's `chrome` flag, re-read on every
    // NavigationEnd. A bare `{ navigate }` stub throws on construction.
    routerEvents = new Subject<unknown>();
    routerRoot = { data: {}, firstChild: null } as any;
    mockRouter = {
      navigate: vi.fn(),
      events: routerEvents.asObservable(),
      routerState: { snapshot: { get root() { return routerRoot; } } },
    };
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

  /** Stand-in for the admin console's nav: this spec is about *which* body
   *  the sidenav renders, not what the console puts in it. The real one
   *  pulls the marketplace service (and its badge fetch) in with it. */
  @Component({ selector: 'app-admin-nav', template: '<p>admin nav</p>' })
  class AdminNavStub {}

  @Component({ selector: 'app-notification-bell', template: '<button>bell</button>' })
  class NotificationBellStub {}

  let mockUserService: any;
  let mockAgentService: any;
  beforeEach(() => {
    TestBed.resetTestingModule();
    // The overrides live on the TestBed that was just reset.
    stubsApplied = false;
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

  /**
   * Swap the sidenav's real children for stubs.
   *
   * Idempotent, and separate from `renderSidenav`, because `overrideComponent`
   * throws once the test module has been instantiated — and a test that drives
   * the router has to `TestBed.inject(Router)` before it renders anything.
   */
  let stubsApplied = false;
  async function applyStubs() {
    if (stubsApplied) return (await import('./sidenav')).Sidenav;

    const { Sidenav } = await import('./sidenav');
    const { SessionList } = await import('./components/session-list/session-list');
    const { UserDropdownComponent } = await import('../topnav/components/user-dropdown.component');
    const { AdminNav } = await import('../../admin/admin-nav');
    const { NotificationBellComponent } = await import('../notification-bell/notification-bell.component');

    TestBed.overrideComponent(Sidenav, {
      remove: { imports: [SessionList, UserDropdownComponent, AdminNav, NotificationBellComponent] },
      add: { imports: [SessionListStub, UserDropdownStub, AdminNavStub, NotificationBellStub] },
    });
    stubsApplied = true;
    return Sidenav;
  }

  async function renderSidenav() {
    const Sidenav = await applyStubs();

    const fixture = TestBed.createComponent(Sidenav);
    fixture.detectChanges();
    return fixture;
  }

  function agentsNavLink(fixture: ComponentFixture<unknown>): HTMLAnchorElement | undefined {
    const anchors = fixture.nativeElement.querySelectorAll('a[href="/agents"]');
    return anchors.length ? (anchors[0] as HTMLAnchorElement) : undefined;
  }

  /**
   * Projects follow this build's compile-time switch (environments/feature-flags.ts),
   * never a request: present from first paint when on, absent when off.
   */
  it('shows Projects and the notification bell in a build with Projects on', async () => {
    TestBed.overrideProvider(FEATURES, { useValue: { projects: true } });
    const fixture = await renderSidenav();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('a[href="/projects"]')?.textContent).toContain('Projects');
    expect(el.querySelector('app-notification-bell')).not.toBeNull();
  });

  it('has no Projects entry and no bell in a build with Projects off', async () => {
    TestBed.overrideProvider(FEATURES, { useValue: { projects: false } });
    const fixture = await renderSidenav();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('a[href="/projects"]')).toBeNull();
    expect(el.querySelector('app-notification-bell')).toBeNull();
  });

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
  // ── Admin console ─────────────────────────────────────────────────────────────────
  //
  // The console's nav used to be a second column inside the admin page, which left
  // every admin surface squeezed between two navigations — this one, listing
  // conversations that cannot be opened from `/admin`, and that one. It now replaces
  // this one's body, while the frame (logo, collapse control, user menu) stays put so
  // the swap reads as the same sidebar rather than a different screen.
  //
  // Driven through the real router on purpose: the flag is declared once on the parent
  // `/admin` route and every child declares none, so a stubbed snapshot would prove the
  // walk works on a tree Angular never builds. See `route-chrome.spec.ts`.
  describe('admin console takes over the body', () => {
    @Component({ selector: 'app-blank', template: '' })
    class BlankPage {}

    async function navigateTo(url: string) {
      // Before the first inject: see the note on `applyStubs`.
      await applyStubs();
      const router = TestBed.inject(Router);
      router.resetConfig([
        { path: '', component: BlankPage },
        {
          path: 'admin',
          data: { chrome: ADMIN_CHROME },
          children: [{ path: 'costs', component: BlankPage }],
        },
      ]);
      await router.navigate([url]);
    }

    it('shows the chat nav on a chat route', async () => {
      await navigateTo('/');
      const html = (await renderSidenav()).nativeElement as HTMLElement;

      expect(html.querySelector('app-session-list')).not.toBeNull();
      expect(html.querySelector('app-admin-nav')).toBeNull();
    });

    it('swaps the body for the admin nav on an admin route', async () => {
      await navigateTo('/admin/costs');
      const html = (await renderSidenav()).nativeElement as HTMLElement;

      expect(html.querySelector('app-admin-nav')).not.toBeNull();
      // The conversation list is the point: it cannot be opened from inside the
      // console, so holding 18rem for it there was the cost.
      expect(html.querySelector('app-session-list')).toBeNull();
      expect(html.querySelector('a[href="/agents"]')).toBeNull();
    });

    it('follows navigation rather than freezing at construction', async () => {
      await navigateTo('/');
      const fixture = await renderSidenav();
      expect(fixture.nativeElement.querySelector('app-admin-nav')).toBeNull();

      await TestBed.inject(Router).navigate(['/admin/costs']);
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('app-admin-nav')).not.toBeNull();
      expect(fixture.nativeElement.querySelector('app-session-list')).toBeNull();
    });

    it('keeps the frame across the swap', async () => {
      await navigateTo('/admin/costs');
      const html = (await renderSidenav()).nativeElement as HTMLElement;

      expect(html.querySelector('button[aria-label="Collapse sidebar"]')).not.toBeNull();
      expect(html.querySelector('app-user-dropdown')).not.toBeNull();
    });
  });
});
