// sidenav.branding.spec.ts
//
// Property-based test for Property 9 (Sidenav_Component half). See
// design.md "Correctness Properties" for the authoritative property text
// and requirements.md 3.2/3.3/3.4/3.5 for the acceptance criteria validated
// here. The Chat_Greeting_Block half of this property lives in
// `chat-container.component.branding.spec.ts`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { Component, input, output, signal } from '@angular/core';
import fc from 'fast-check';

import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { MemorySpaceService } from '../../memory-spaces/services/memory-space.service';
import { AgentService } from '../../agents/services/agent.service';
import { LEGACY_MIGRATION_HOST } from '../../shared/utils/legacy-migration-host';
import { BrandingService } from '../../../branding/branding.service';

/**
 * Stand-ins for the sidenav's real children (`SessionList`,
 * `UserDropdownComponent`), mirroring the pattern already established in
 * `sidenav.spec.ts`. This spec is about the branding `<img>` markup only,
 * and pulling in the real children would drag their dependency graphs
 * (and HTTP/router chatter) into every one of the 100 property iterations.
 */
@Component({ selector: 'app-session-list', template: '' })
class SessionListStub {}

@Component({ selector: 'app-user-dropdown', template: '' })
class UserDropdownStub {
  readonly user = input<unknown>();
  readonly isAdmin = input<boolean>(false);
  readonly logout = output<void>();
}

/**
 * Arbitrary for an already-normalized app name, as `BrandingService`
 * would expose it to a consuming component: BrandingService's own
 * normalization (Property 5, task 4.3) guarantees the exposed `appName`
 * is never absent/empty/whitespace-only — it is either the valid
 * configured `App_Name` or the fixed `DEFAULT_ALT_LABEL`. This test does
 * not re-test that normalization; it generates any string meeting that
 * same guarantee (1-100 chars, >=1 non-whitespace character, matching the
 * App_Name bounds) and asserts the component wiring stays consistent for
 * every such value.
 */
const arbNormalizedAppName = fc
  .string({ minLength: 1, maxLength: 100 })
  .filter((s) => /\S/.test(s));

interface MockBrandingService {
  logo: { light: string; dark: string };
  appName: string;
  greetingTemplates: readonly string[];
  fallbackGreetings: readonly string[];
  configErrors: readonly unknown[];
}

describe('Sidenav — Property 9: Logo alt text equals normalized app name', () => {
  let mockBranding: MockBrandingService;

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockBranding = {
      logo: { light: 'img/logo-light.png', dark: 'img/logo-dark.png' },
      appName: 'Initial App Name',
      greetingTemplates: [],
      fallbackGreetings: [],
      configErrors: [],
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: { navigate: vi.fn() } },
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
            // No current user: keeps the user-dropdown block (and its stub)
            // out of the rendered tree entirely, leaving only the two
            // branding <img> elements to query.
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
        { provide: LEGACY_MIGRATION_HOST, useValue: false },
        { provide: BrandingService, useValue: mockBranding },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * One-time async setup that resolves the lazy `Sidenav` chunk and swaps
   * in the child stubs. Kept separate from fixture creation so the
   * property below can create a fresh fixture per iteration synchronously
   * (see the comment at the call site for why a fresh fixture per
   * iteration is required here).
   */
  async function prepareSidenavModule() {
    const { Sidenav } = await import('./sidenav');
    const { SessionList } = await import('./components/session-list/session-list');
    const { UserDropdownComponent } = await import('../topnav/components/user-dropdown.component');

    TestBed.overrideComponent(Sidenav, {
      remove: { imports: [SessionList, UserDropdownComponent] },
      add: { imports: [SessionListStub, UserDropdownStub] },
    });

    return Sidenav;
  }

  // Feature: branding-customization, Property 9: Logo alt text equals normalized app name
  // Validates: Requirements 3.2, 3.3, 3.4, 3.5
  it('every branding logo <img> has alt text identical to the normalized app name, for any app name', async () => {
    // Resolve the lazy `Sidenav` chunk and child-stub overrides once, up
    // front, so each of the 100 property iterations below can create a
    // *fresh* component fixture synchronously. A fresh fixture per
    // iteration — with `appName` set on the shared mock BEFORE
    // `TestBed.createComponent` — avoids Angular's dev-mode `NG0100`
    // re-check: reusing one fixture and mutating the mock's `appName`
    // between `detectChanges()` calls trips the "Expression has changed
    // after it was checked" guard, because the previously-checked binding
    // value and the live mock value briefly disagree across the two
    // internal check passes `detectChanges()` performs.
    const Sidenav = await prepareSidenavModule();

    fc.assert(
      fc.property(arbNormalizedAppName, (appName) => {
        mockBranding.appName = appName;
        const fixture = TestBed.createComponent(Sidenav);
        fixture.detectChanges();

        try {
          const imgs: HTMLImageElement[] = Array.from(
            fixture.nativeElement.querySelectorAll('img'),
          );

          // Sanity: the logo markup (light + dark variants) is actually present.
          expect(imgs.length).toBeGreaterThan(0);

          for (const img of imgs) {
            expect(img.alt).toBe(appName);
          }
        } finally {
          // Undestroyed fixtures from earlier iterations stay attached to
          // the same TestBed-managed injector tree; leaving them around
          // means a later iteration's `detectChanges()` sweeps them back
          // in for a no-changes re-check against the (by-then-mutated)
          // shared mock, tripping NG0100 spuriously. Destroy after each
          // iteration so only the current fixture is ever live.
          fixture.destroy();
        }
      }),
      { numRuns: 100 },
    );
  }, 30_000);
});
