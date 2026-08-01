import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';

import { AgentDetailPage } from './agent-detail.page';
import { AgentApiService } from '../services/agent-api.service';
import { AgentPinService } from '../services/agent-pin.service';
import { Agent, AgentRunnability } from '../models/agent.model';

/**
 * The load path, driven through the router.
 *
 * ⚠️ Route the component, don't construct it. `id` is an `input.required` bound by
 * `withComponentInputBinding()`, which the router sets *after* construction — so this
 * page's one interesting timing question ("is `id` readable yet?") only exists when the
 * real binder answers it. Phase 3 shipped `load()` from the constructor and every user
 * got "Failed to load this agent." with no request behind it: `this.id()` threw NG0950,
 * and `load()`'s own try/catch turned that into the banner. A spec that handed `id` in at
 * construction time would have painted green over all of it, which is why the assertions
 * below are "was `getAgent` called" and "is the banner absent" rather than anything about
 * the rendered agent — those two are what the bug actually broke.
 */

const AGENT: Agent = {
  agentId: 'agt-123',
  ownerName: 'Ada Lovelace',
  name: 'Research Assistant',
  description: 'Finds and summarises papers.',
  bindings: [],
  visibility: 'PUBLIC',
  tags: [],
  starters: ['Summarise this paper'],
  usageCount: 4,
  status: 'COMPLETE',
  createdAt: '2026-07-01T00:00:00Z',
  updatedAt: '2026-07-20T00:00:00Z',
};

const RUNNABILITY: AgentRunnability = { agentId: 'agt-123', state: 'ready', missing: [] };

describe('AgentDetailPage — load path', () => {
  const api = {
    getAgent: vi.fn(),
    getRunnability: vi.fn(),
    reportAgent: vi.fn(),
  };

  const pinService = {
    load: vi.fn().mockResolvedValue([]),
    toggle: vi.fn().mockResolvedValue(undefined),
    isPinned: vi.fn().mockReturnValue(false),
    isLocked: vi.fn().mockReturnValue(false),
    error: vi.fn().mockReturnValue(null),
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.getAgent.mockReturnValue(of(AGENT));
    api.getRunnability.mockReturnValue(of(RUNNABILITY));

    TestBed.configureTestingModule({
      providers: [
        // `withComponentInputBinding()` is the subject here, not incidental setup —
        // without it the route param never reaches `id` and the page cannot load at all.
        provideRouter(
          [{ path: 'agents/:id', component: AgentDetailPage }],
          withComponentInputBinding(),
        ),
        { provide: AgentApiService, useValue: api },
        { provide: AgentPinService, useValue: pinService },
        // Only the report control opens it; stubbed so no overlay reaches the document.
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  /**
   * Navigate, then let the load promises settle before rendering. `load()` awaits two
   * `firstValueFrom` calls, so even synchronous `of()` stubs land a microtask later than
   * the navigation resolves; without the drain the fixture is still on its spinner.
   */
  async function open(url = '/agents/agt-123') {
    const harness = await RouterTestingHarness.create();
    const page = await harness.navigateByUrl(url, AgentDetailPage);
    await new Promise((resolve) => setTimeout(resolve, 0));
    harness.detectChanges();
    return { harness, page };
  }

  function loadBanner(harness: RouterTestingHarness): HTMLElement | undefined {
    const alerts: HTMLElement[] = Array.from(
      harness.routeNativeElement?.querySelectorAll('[role="alert"]') ?? [],
    );
    // The pin and report controls also render alerts; only the page-level one is in play.
    return alerts.find((el) => el.textContent?.includes('Failed to load this agent.'));
  }

  it('requests the agent the router bound to `id`', async () => {
    await open('/agents/agt-123');

    expect(api.getAgent).toHaveBeenCalledTimes(1);
    expect(api.getAgent).toHaveBeenCalledWith('agt-123');
  });

  it('does not show the error banner when the read succeeds', async () => {
    const { harness, page } = await open();

    expect(page.error()).toBeNull();
    expect(loadBanner(harness)).toBeUndefined();
    expect(page.loading()).toBe(false);
    expect(harness.routeNativeElement?.querySelector('h1')?.textContent).toContain(
      'Research Assistant',
    );
  });

  it('asks for runnability separately, for the same id', async () => {
    const { page } = await open();

    expect(api.getRunnability).toHaveBeenCalledWith('agt-123');
    expect(page.runnability()).toEqual(RUNNABILITY);
  });

  /**
   * The negative control. Without it, "banner absent" above could pass on a page that
   * can never show a banner at all.
   */
  it('shows the error banner when the read actually fails', async () => {
    api.getAgent.mockReturnValue(throwError(() => ({ error: { detail: 'Agent not found.' } })));

    const { harness, page } = await open();

    expect(page.error()).toBe('Agent not found.');
    expect(harness.routeNativeElement?.querySelector('[role="alert"]')?.textContent).toContain(
      'Agent not found.',
    );
  });

  /** Runnability is advisory (D6): its failure must not error the whole page. */
  it('keeps the page when only runnability fails', async () => {
    api.getRunnability.mockReturnValue(throwError(() => new Error('nope')));

    const { harness, page } = await open();

    expect(page.agent()).toEqual(AGENT);
    expect(page.runnability()).toBeNull();
    expect(page.error()).toBeNull();
    expect(loadBanner(harness)).toBeUndefined();
  });
});
