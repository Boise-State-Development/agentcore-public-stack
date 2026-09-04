import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideLocationMocks } from '@angular/common/testing';
import { provideRouter } from '@angular/router';
import { Component, input, output, signal } from '@angular/core';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { MemorySpaceService } from '../../memory-spaces/services/memory-space.service';
import { AgentService } from '../../agents/services/agent.service';
import { LEGACY_MIGRATION_HOST } from '../../shared/utils/legacy-migration-host';

/**
 * Feature: branding-customization
 *
 * Covers the two example/edge-case scenarios called out in the design's
 * "Testing Strategy" and "Error Handling" sections for the Sidenav_Component
 * branding logo:
 *
 *  1. Theme swap happens via CSS only (both `<img>` elements are always
 *     present in the DOM at once, differentiated only by `dark:` classes) —
 *     no navigation/reload and no JS branch on theme state for the
 *     light/dark choice itself (Requirements 2.4, 2.5, 2.6, 7.3).
 *  2. The `(error)` handler reveals a same-dimension placeholder and keeps
 *     surrounding content (e.g. the collapse button) rendered (Requirement 2.8).
 *
 * Child components (`SessionList`, `UserDropdownComponent`) are stubbed —
 * this spec is about the logo/theme/error-handling markup, not their
 * dependency graphs.
 */
describe('Sidenav — branding logo theme swap and error handling', () => {
  @Component({ selector: 'app-session-list', template: '' })
  class SessionListStub {}

  @Component({ selector: 'app-user-dropdown', template: '' })
  class UserDropdownStub {
    readonly user = input<unknown>();
    readonly isAdmin = input<boolean>(false);
    readonly logout = output<void>();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();

    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideLocationMocks(),
        {
          provide: SessionService,
          useValue: {
            currentSession: signal({
              sessionId: 's1',
              userId: 'u1',
              title: 'T',
              status: 'active' as const,
              createdAt: '',
              lastMessageAt: '',
              messageCount: 0,
            }),
            hasCurrentSession: signal(true),
          },
        },
        { provide: BffSessionService, useValue: { logout: vi.fn() } },
        {
          provide: SidenavService,
          useValue: { isCollapsed: signal(false), close: vi.fn(), toggleCollapsed: vi.fn() },
        },
        {
          provide: UserService,
          useValue: {
            hasAnyRole: vi.fn().mockReturnValue(false),
            currentUser: signal(null),
            isAdmin: signal(false),
            canAccessAdmin: signal(false),
          },
        },
        {
          provide: MemorySpaceService,
          useValue: { accessible$: signal<boolean | null>(false), loadSpaces: vi.fn().mockResolvedValue(undefined) },
        },
        {
          provide: AgentService,
          useValue: { accessible$: signal<boolean | null>(false), loadAgents: vi.fn().mockResolvedValue(undefined) },
        },
        { provide: LEGACY_MIGRATION_HOST, useFactory: () => false },
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

  it('renders both light and dark logo <img> elements simultaneously, swapped only via dark: CSS classes', async () => {
    const fixture = await renderSidenav();
    const html = fixture.nativeElement as HTMLElement;

    const images = html.querySelectorAll('img');
    expect(images.length).toBe(2);

    const lightImg = Array.from(images).find((img) => img.className.includes('dark:hidden'));
    const darkImg = Array.from(images).find((img) => img.className.includes('hidden') && img.className.includes('dark:block'));

    // Both images are present in the DOM at once — the swap is CSS-driven
    // (via class presence), not JS-driven (no *ngIf/@if branching on a
    // theme signal for the light/dark choice itself).
    expect(lightImg).toBeDefined();
    expect(darkImg).toBeDefined();
    expect(lightImg).not.toBe(darkImg);
  });

  it('reveals a same-dimension placeholder on (error) and keeps surrounding content rendered', async () => {
    const fixture = await renderSidenav();
    const html = fixture.nativeElement as HTMLElement;
    const component = fixture.componentInstance;

    // Precondition: images present, placeholder absent, failure flag false.
    expect(html.querySelectorAll('img').length).toBe(2);
    expect(component['logoLoadFailed']()).toBe(false);
    expect(html.querySelector('[role="img"][aria-label*="logo failed to load"]')).toBeNull();

    const lightImg = html.querySelectorAll('img')[0];
    lightImg.dispatchEvent(new Event('error'));

    expect(component['logoLoadFailed']()).toBe(true);

    fixture.detectChanges();

    // Placeholder now present, identified by its aria-label.
    const placeholder = html.querySelector('[role="img"][aria-label*="logo failed to load"]');
    expect(placeholder).not.toBeNull();

    // The original <img> elements are swapped out.
    expect(html.querySelectorAll('img').length).toBe(0);

    // Surrounding content (the collapse button) still renders normally.
    const collapseButton = html.querySelector('button[aria-label="Collapse sidebar"]');
    expect(collapseButton).not.toBeNull();
  });
});
