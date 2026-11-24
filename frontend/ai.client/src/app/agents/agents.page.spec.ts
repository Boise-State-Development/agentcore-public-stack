import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { AgentsPage } from './agents.page';
import { AgentService } from './services/agent.service';
import { AgentPinService } from './services/agent-pin.service';
import { Agent } from './models/agent.model';
import { Router } from '@angular/router';
import { ToastService } from '../services/toast/toast.service';
import { LocalSettingsService } from '../services/local-settings.service';
import { ConfigService } from '../services/config.service';
import { environment } from '../../environments/environment';
import { FEATURES } from '../services/features';
import { ListArrangementService } from '../services/list-arrangement/list-arrangement.service';
import { listArrangementStub } from '../../testing/list-arrangement';

const AGENT = {
  agentId: 'ast-a1b2c3d4e5f6',
  name: 'Rubric Builder',
  description: 'Builds rubrics',
  visibility: 'PRIVATE',
  status: 'COMPLETE',
  userPermission: 'owner',
} as unknown as Agent;

/** The pin list as the page reads it: which ids are pinned, and which of those are locked. */
function pinServiceStub() {
  const pinned = signal(new Set<string>());
  const locked = signal(new Set<string>());
  const error = signal<string | null>(null);
  return {
    pinned,
    locked,
    error,
    load: vi.fn().mockResolvedValue([]),
    isPinned: (id: string) => pinned().has(id),
    isLocked: (id: string) => locked().has(id),
    pin: vi.fn(async (id: string) => {
      pinned.set(new Set([...pinned(), id]));
    }),
    unpin: vi.fn(async (id: string) => {
      const next = new Set(pinned());
      next.delete(id);
      pinned.set(next);
    }),
    toggle: vi.fn(async (id: string) => {
      const next = new Set(pinned());
      if (next.has(id)) next.delete(id);
      else next.add(id);
      pinned.set(next);
    }),
  };
}

describe('AgentsPage row actions', () => {
  const viewMode = signal<'grid' | 'list'>('grid');
  let pins: ReturnType<typeof pinServiceStub>;
  let arrangements: ReturnType<typeof listArrangementStub>;
  let organizeLists = false;

  beforeEach(() => {
    TestBed.resetTestingModule();
    viewMode.set('grid');
    pins = pinServiceStub();
    arrangements = listArrangementStub();
    organizeLists = false;
    TestBed.configureTestingModule({
      imports: [AgentsPage],
      providers: [
        provideRouter([]),
        {
          provide: AgentService,
          useValue: {
            agents$: signal([AGENT]),
            loading$: signal(false),
            error$: signal(null),
            loadAgents: vi.fn().mockResolvedValue(undefined),
            duplicateAgent: vi.fn().mockResolvedValue({ ...AGENT, agentId: 'ast-copy' }),
          },
        },
        { provide: AgentPinService, useValue: pins },
        {
          provide: LocalSettingsService,
          useValue: { agentsViewMode: viewMode, setAgentsViewMode: (v: 'grid' | 'list') => viewMode.set(v) },
        },
        { provide: ConfigService, useValue: { appApiUrl: () => '' } },
        { provide: ListArrangementService, useFactory: () => arrangements },
        { provide: FEATURES, useFactory: () => ({ ...environment.features, organizeLists }) },
      ],
    });
  });

  function render(mode: 'grid' | 'list') {
    viewMode.set(mode);
    const fixture = TestBed.createComponent(AgentsPage);
    fixture.detectChanges();
    return fixture;
  }

  const pinButton = (el: HTMLElement) =>
    el.querySelector<HTMLButtonElement>('button[aria-label="Pin Rubric Builder to sidebar"]');

  // Icon-only buttons: the tooltip only describes them (aria-describedby), so without
  // a label a screen reader announced every Chat, Edit, Share and Delete as "button".
  it.each(['grid', 'list'] as const)('names every icon-only action after its agent (%s view)', mode => {
    const el = render(mode).nativeElement as HTMLElement;

    const labels = Array.from(el.querySelectorAll('button'))
      .filter(b => b.querySelector('ng-icon') && !b.textContent?.trim())
      .map(b => b.getAttribute('aria-label'));

    expect(labels).toEqual([
      'Chat with Rubric Builder',
      'Pin Rubric Builder to sidebar',
      'Edit Rubric Builder',
      'Duplicate Rubric Builder',
      'Share Rubric Builder',
      'Delete Rubric Builder',
    ]);
  });

  // ── pin to sidebar (#1897) ──────────────────────────────────────────────────────
  it('loads the pin list so each row knows its state', () => {
    render('grid');
    expect(pins.load).toHaveBeenCalled();
  });

  it.each(['grid', 'list'] as const)('pins a private, unpublished agent from its row (%s view)', async mode => {
    const fixture = render(mode);
    const button = pinButton(fixture.nativeElement)!;
    expect(button.getAttribute('aria-pressed')).toBe('false');

    button.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(pins.toggle).toHaveBeenCalledWith('ast-a1b2c3d4e5f6');
    expect(pinButton(fixture.nativeElement)!.getAttribute('aria-pressed')).toBe('true');
  });

  it('offers the pin on an agent shared with you as a viewer', () => {
    // Pinning is gated on reach, not on editing rights: everything listed here is reachable.
    TestBed.overrideProvider(AgentService, {
      useValue: {
        agents$: signal([{ ...AGENT, userPermission: 'viewer', isSharedWithMe: true }]),
        loading$: signal(false),
        error$: signal(null),
        loadAgents: vi.fn().mockResolvedValue(undefined),
      },
    });
    expect(pinButton(render('list').nativeElement)).toBeTruthy();
  });

  it.each(['grid', 'list'] as const)('shows a lock in place of the toggle for a locked role pin (%s view)', mode => {
    pins.pinned.set(new Set([AGENT.agentId]));
    pins.locked.set(new Set([AGENT.agentId]));
    const el = render(mode).nativeElement as HTMLElement;

    expect(pinButton(el)).toBeNull();
    expect(el.querySelector('ng-icon[name="heroLockClosed"]')).toBeTruthy();
    expect(el.textContent).toContain("Rubric Builder is pinned for your role and can't be removed");
  });

  it('says why when the pin fails', async () => {
    const fixture = render('grid');
    const toastError = vi.spyOn(TestBed.inject(ToastService), 'error');
    pins.toggle.mockImplementationOnce(async () => {
      pins.error.set('Too many pins.');
      throw new Error('409');
    });

    await fixture.componentInstance.onTogglePin(AGENT);

    expect(toastError).toHaveBeenCalledWith('Could not update pin', 'Too many pins.');
    expect(fixture.componentInstance.pinningId()).toBeNull();
  });

  it("duplicates and opens the copy's edit page", async () => {
    const fixture = TestBed.createComponent(AgentsPage);
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const info = vi.spyOn(TestBed.inject(ToastService), 'info');
    await fixture.componentInstance.onDuplicate(AGENT);
    expect(TestBed.inject(AgentService).duplicateAgent).toHaveBeenCalledWith('ast-a1b2c3d4e5f6');
    expect(navigate).toHaveBeenCalledWith(['/agents', 'ast-copy', 'edit']);
    expect(info).toHaveBeenCalledTimes(1);
  });

  // ── Hide and filters (docs/specs/agent-project-organization.md) ────────────────
  const SHARED = { ...AGENT, agentId: 'ast-0000000000bb', name: 'Lab Helper', userPermission: 'viewer', isSharedWithMe: true } as Agent;

  function renderOrganized(mode: 'grid' | 'list' = 'grid', agents: Agent[] = [AGENT, SHARED]) {
    organizeLists = true;
    TestBed.overrideProvider(AgentService, {
      useValue: { agents$: signal(agents), loading$: signal(false), error$: signal(null), loadAgents: vi.fn().mockResolvedValue(undefined) },
    });
    return render(mode);
  }

  const shownNames = (el: HTMLElement) =>
    [...el.querySelectorAll('h3, li span.truncate.font-semibold')].map(n => n.textContent?.trim());
  const chips = (el: HTMLElement) => [...el.querySelectorAll('[role=radiogroup][aria-label=Show] [role=radio]')];
  const chip = (el: HTMLElement, label: string) => chips(el).find(b => b.textContent?.trim() === label) as HTMLButtonElement;
  const hideButton = (el: HTMLElement, name: string) => el.querySelector<HTMLButtonElement>(`button[aria-label="Hide ${name}"]`);

  it('offers no filters and no Hide while the flag is off', () => {
    const el = render('grid').nativeElement as HTMLElement;
    expect(chips(el)).toHaveLength(0);
    expect(hideButton(el, 'Rubric Builder')).toBeNull();
  });

  it.each(['grid', 'list'] as const)('splits Mine from Shared with me (%s view)', mode => {
    const fixture = renderOrganized(mode);
    const el = fixture.nativeElement as HTMLElement;
    expect(arrangements.open).toHaveBeenCalledWith('agents');
    expect(chips(el).map(b => b.textContent?.trim())).toEqual(['All', 'Mine', 'Shared with me']);
    expect(shownNames(el)).toEqual(['Rubric Builder', 'Lab Helper']);

    chip(el, 'Mine').click();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Rubric Builder']);
    chip(el, 'Shared with me').click();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Lab Helper']);
  });

  it.each(['grid', 'list'] as const)('names the Hide action after its agent (%s view)', mode => {
    const el = renderOrganized(mode, [AGENT]).nativeElement as HTMLElement;
    const labels = Array.from(el.querySelectorAll('button'))
      .filter(b => b.querySelector('ng-icon') && !b.textContent?.trim())
      .map(b => b.getAttribute('aria-label'));
    expect(labels).toContain('Hide Rubric Builder');
  });

  it('hides a pinned agent by unpinning it first, and Undo puts both back', async () => {
    pins.pinned.set(new Set([SHARED.agentId]));
    const fixture = renderOrganized();
    const el = fixture.nativeElement as HTMLElement;
    const info = vi.spyOn(TestBed.inject(ToastService), 'info');

    hideButton(el, 'Lab Helper')!.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(pins.unpin).toHaveBeenCalledWith(SHARED.agentId);
    expect(arrangements.setHidden).toHaveBeenCalledWith('agents', SHARED.agentId, true, expect.any(Array));
    expect(shownNames(el)).toEqual(['Rubric Builder']);
    expect(chip(el, 'Hidden (1)')).toBeTruthy();
    const [title, message, options] = info.mock.calls[0];
    expect(title).toBe('Hid Lab Helper');
    expect(message).toContain('unpinned from your sidebar');

    options!.action!.handler();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Rubric Builder', 'Lab Helper']);
    expect(pins.pin).toHaveBeenCalledWith(SHARED.agentId);
  });

  it('lists hidden agents under Hidden and unhides them there', async () => {
    arrangements.hide('agents', AGENT.agentId);
    const fixture = renderOrganized();
    const el = fixture.nativeElement as HTMLElement;
    chip(el, 'Hidden (1)').click();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Rubric Builder']);

    el.querySelector<HTMLButtonElement>('button[aria-label="Unhide Rubric Builder"]')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.textContent).toContain('Nothing hidden.');
    expect(pins.unpin).not.toHaveBeenCalled();
  });

  it('never offers Hide on a locked role pin', () => {
    pins.pinned.set(new Set([AGENT.agentId]));
    pins.locked.set(new Set([AGENT.agentId]));
    const el = renderOrganized('grid', [AGENT]).nativeElement as HTMLElement;
    expect(hideButton(el, 'Rubric Builder')).toBeNull();
  });

  it('says so when a hide fails', async () => {
    const fixture = renderOrganized();
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    arrangements.fail();
    await fixture.componentInstance.onToggleHidden(AGENT);
    expect(error).toHaveBeenCalledWith('Could not hide agent', 'Please try again.');
    expect(fixture.componentInstance.hidingId()).toBeNull();
  });

  // ── Sort, Arrange and New (docs/specs/agent-project-organization.md §6.3, D6) ────
  const sortRadio = (el: HTMLElement, label: string) =>
    [...el.querySelectorAll('[aria-label=Sort] [role=radio]')].find(b => b.textContent?.trim() === label) as HTMLButtonElement;

  it('sorts by name and back to recently added', async () => {
    const fixture = renderOrganized();
    const el = fixture.nativeElement as HTMLElement;
    sortRadio(el, 'Name').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Lab Helper', 'Rubric Builder']);
    expect(sortRadio(el, 'Name').getAttribute('aria-checked')).toBe('true');

    sortRadio(el, 'Recently added').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(shownNames(el)).toEqual(['Rubric Builder', 'Lab Helper']);
  });

  it('arranges, saves as Custom, and unpins an agent hidden there', async () => {
    pins.pinned.set(new Set([AGENT.agentId]));
    const fixture = renderOrganized();
    const el = fixture.nativeElement as HTMLElement;
    [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Arrange')!.click();
    fixture.detectChanges();

    expect(el.querySelector('app-arrange-list')).toBeTruthy();
    el.querySelector<HTMLButtonElement>('button[aria-label="Move Lab Helper up"]')!.click();
    el.querySelector<HTMLInputElement>('input[aria-label="Hide Rubric Builder"]')!.click();
    fixture.detectChanges();
    [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Done')!.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(pins.unpin).toHaveBeenCalledWith(AGENT.agentId);
    expect(arrangements.saveArranged).toHaveBeenCalledWith(
      'agents',
      [SHARED.agentId, AGENT.agentId],
      new Set([AGENT.agentId]),
      [AGENT.agentId, SHARED.agentId],
    );
    expect(el.querySelector('app-arrange-list')).toBeNull();
    expect(sortRadio(el, 'Custom').getAttribute('aria-checked')).toBe('true');
    expect(shownNames(el)).toEqual(['Lab Helper']);
  });

  it('badges an agent shared since the user last arranged, first, as New', () => {
    arrangements.arrangements.agents.set({ sort: 'custom', order: [AGENT.agentId], hidden: [], updatedAt: '2026-11-01T00:00:00Z' });
    const el = renderOrganized('list').nativeElement as HTMLElement;
    expect(shownNames(el)).toEqual(['Lab Helper', 'Rubric Builder']);
    const rows = [...el.querySelectorAll('main li')];
    expect(rows[0].textContent).toContain('New');
    expect(rows[1].textContent).not.toContain('New');
  });
});
