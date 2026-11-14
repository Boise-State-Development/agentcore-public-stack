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

  beforeEach(() => {
    TestBed.resetTestingModule();
    viewMode.set('grid');
    pins = pinServiceStub();
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
});
