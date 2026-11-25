import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { NEVER, of, throwError } from 'rxjs';
import { ScheduleDetailPage, compactTokens } from './schedule-detail.page';
import { ScheduleService } from '../services/schedule.service';
import { ScheduleApiService } from '../services/schedule-api.service';
import { ModelService } from '../../session/services/model/model.service';
import { ToastService } from '../../services/toast/toast.service';
import { FEATURES } from '../../services/features';
import { ScheduleRun, ScheduledPrompt, WatchState } from '../models/schedule.model';
import { expectNamedDialog } from '../../../testing/cdk-dialog';

/**
 * A schedule's own page (docs/specs/quiet-watcher.md §3.4): its header, every run
 * (quiet ones included) with a one-line last-24-hours summary, and for a watcher
 * what it is tracking, with Start over behind a confirmation.
 */

const NOW = Date.now();
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

function stubSchedule(overrides: Partial<ScheduledPrompt> = {}): ScheduledPrompt {
  return {
    scheduleId: 'sched-1',
    assistantId: null,
    label: 'GitHub watch',
    promptText: 'Check my notifications',
    cadence: 'interval',
    hourLocal: 0,
    weekday: null,
    intervalValue: 15,
    intervalUnit: 'minutes',
    timezone: 'America/Boise',
    state: 'active',
    runsToday: 3,
    maxRunsPerDay: 96,
    enabledTools: null,
    deliverEmail: false,
    mode: 'watch',
    modelId: 'us.small',
    createdAt: '2026-11-25T00:00:00Z',
    updatedAt: '2026-11-25T00:00:00Z',
    ...overrides,
  };
}

function run(overrides: Partial<ScheduleRun>): ScheduleRun {
  return {
    runId: 'r1',
    scheduleId: 'sched-1',
    status: 'quiet',
    itemCount: 0,
    sessionId: null,
    finishedAt: ago(10),
    inputTokens: 2100,
    outputTokens: 120,
    cacheReadTokens: 0,
    ...overrides,
  };
}

const RUNS: ScheduleRun[] = [
  run({ runId: 'r-old', status: 'quiet', finishedAt: ago(60) }),
  run({ runId: 'r-new', status: 'notify', itemCount: 2, sessionId: 'sess-9', finishedAt: ago(5), inputTokens: 40_500 }),
  run({ runId: 'r-mid', status: 'error', finishedAt: ago(30) }),
  run({ runId: 'r-yesterday', status: 'notify', itemCount: 1, sessionId: 'sess-1', finishedAt: ago(26 * 60) }),
];

const STATE: WatchState = {
  cursors: { notifications_since: '2026-11-25T10:00:00Z' },
  tracking: ['PR 2401 awaiting review', 'Issue 1798 promotion list'],
  seen: ['a', 'b', 'c'],
  version: 4,
  lastRunAt: ago(5),
  updatedAt: ago(5),
};

describe('ScheduleDetailPage', () => {
  let fixture: ComponentFixture<ScheduleDetailPage>;
  let el: HTMLElement;
  let api: {
    runs: ReturnType<typeof vi.fn>;
    watchState: ReturnType<typeof vi.fn>;
    clearWatchState: ReturnType<typeof vi.fn>;
  };
  let toast: { success: ReturnType<typeof vi.fn>; error: ReturnType<typeof vi.fn> };

  async function mount(opts: {
    schedule?: ScheduledPrompt | undefined;
    scheduleError?: unknown;
    runs?: ScheduleRun[];
    state?: WatchState | null;
    runsPending?: boolean;
  } = {}): Promise<void> {
    TestBed.resetTestingModule();
    api = {
      runs: vi.fn().mockReturnValue(opts.runsPending ? NEVER : of({ runs: opts.runs ?? RUNS })),
      watchState: vi.fn().mockReturnValue(of({ state: opts.state === undefined ? STATE : opts.state })),
      clearWatchState: vi.fn().mockReturnValue(of(null)),
    };
    toast = { success: vi.fn(), error: vi.fn() };
    const schedule = 'schedule' in opts ? opts.schedule : stubSchedule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        {
          provide: ScheduleService,
          useValue: {
            getSchedule: opts.scheduleError
              ? vi.fn().mockRejectedValue(opts.scheduleError)
              : vi.fn().mockResolvedValue(schedule),
          },
        },
        { provide: ScheduleApiService, useValue: api },
        { provide: ModelService, useValue: { modelNameFor: (id: string) => (id === 'us.small' ? 'Small Model' : null) } },
        { provide: ToastService, useValue: toast },
        { provide: FEATURES, useValue: { proactiveRuns: true } },
      ],
    });
    fixture = TestBed.createComponent(ScheduleDetailPage);
    fixture.componentRef.setInput('scheduleId', 'sched-1');
    el = fixture.nativeElement;
    await settle(opts.runsPending);
  }

  /** `whenStable` waits on pending resources, so a load that never ends is ticked instead. */
  async function settle(pending = false): Promise<void> {
    for (let i = 0; i < 4; i++) {
      fixture.detectChanges();
      if (pending) await new Promise((resolve) => setTimeout(resolve, 0));
      else await fixture.whenStable();
    }
    fixture.detectChanges();
  }

  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  const text = (selector: string) => el.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim() ?? null;

  it('shows the header: label, cadence, mode and model', async () => {
    await mount();
    expect(text('h1')).toBe('GitHub watch');
    expect(el.textContent).toContain('Every 15 minutes · America/Boise');
    expect(text('[data-testid="watching-badge"]')).toBe('Watching');
    expect(text('[data-testid="schedule-mode"]')).toBe('Watches, and only tells you when something is new');
    expect(text('[data-testid="schedule-model"]')).toBe('Small Model');
  });

  it('says Default for a schedule with no model of its own, and no badge for a standard one', async () => {
    await mount({ schedule: stubSchedule({ mode: 'standard', modelId: null }) });
    expect(text('[data-testid="schedule-model"]')).toBe('Default');
    expect(el.querySelector('[data-testid="watching-badge"]')).toBeNull();
    expect(el.textContent).not.toContain('What it’s tracking');
    // A standard schedule never asks for watch state.
    expect(api.watchState).not.toHaveBeenCalled();
  });

  it('lists the runs newest first, worded, with tokens and a link to each run’s conversation', async () => {
    await mount();
    expect(api.runs).toHaveBeenCalledWith('sched-1');
    const rows = [...el.querySelectorAll('[data-testid="run-list"] > li')];
    expect(rows.map((row) => row.querySelector('span')!.textContent!.trim())).toEqual([
      'Found 2',
      'Failed',
      'Nothing new',
      'Found 1',
    ]);
    expect(rows[0].textContent).toContain('5m ago');
    expect(rows[0].textContent).toContain('40.5k in · 120 out · 0 cached tokens');
    expect(rows[0].querySelector('a')!.getAttribute('href')).toBe('/s/sess-9');
    // A quiet run has no conversation to open.
    expect(rows[2].querySelector('a')).toBeNull();
  });

  it('sums up the last 24 hours above the list', async () => {
    await mount();
    expect(text('[data-testid="day-summary"]')).toBe('Last 24 hours: Checked 3 times · found something 1 time');
  });

  it('shows what a watcher is tracking: its lines, its cursors and how many findings it remembers', async () => {
    await mount();
    expect([...el.querySelectorAll('[data-testid="tracking-list"] li')].map((li) => li.textContent!.trim())).toEqual([
      'PR 2401 awaiting review',
      'Issue 1798 promotion list',
    ]);
    expect(text('[data-testid="cursor-list"] dt')).toBe('notifications_since');
    expect(text('[data-testid="cursor-list"] dd')).toBe('2026-11-25T10:00:00Z');
    expect(text('[data-testid="seen-count"]')).toContain('3 findings');
  });

  it('has empty states for no runs and no state, from real answers, not 404s', async () => {
    await mount({ runs: [], state: null });
    expect(text('[data-testid="runs-empty"]')).toContain('No runs yet');
    expect(text('[data-testid="runs-empty"]')).toContain('including the ones that found nothing new');
    expect(text('[data-testid="state-empty"]')).toContain('Nothing yet');
    expect(el.querySelector('[data-testid="day-summary"]')).toBeNull();
    // Nothing to clear, so no Start over.
    expect([...el.querySelectorAll('button')].some((b) => b.textContent!.includes('Start over'))).toBe(false);
  });

  it('shows skeleton rows with a status line while the runs first load', async () => {
    await mount({ runsPending: true });
    const status = [...el.querySelectorAll('[role="status"]')].map((s) => s.textContent!.trim());
    expect(status).toContain('Loading run history…');
    const skeleton = el.querySelector('section[aria-labelledby="schedule-runs"] [aria-hidden="true"]');
    expect(skeleton?.classList.contains('animate-pulse')).toBe(true);
    expect(skeleton?.querySelectorAll('li').length).toBe(3);
  });

  it('says a deleted schedule no longer exists', async () => {
    await mount({ schedule: undefined });
    expect(el.textContent).toContain('This schedule no longer exists.');
    expect(api.runs).not.toHaveBeenCalled();
  });

  it('offers Try again for a schedule that failed to load for another reason', async () => {
    await mount({ scheduleError: new HttpErrorResponse({ status: 500 }) });
    expect(el.textContent).toContain('This schedule couldn’t be loaded.');
  });

  it('shows the run list’s error, not an empty state, when the runs fail', async () => {
    TestBed.resetTestingModule();
    await mount();
    api.runs.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    fixture.componentInstance.runs.reload();
    await settle();
    expect(el.textContent).toContain('The run history couldn’t be loaded.');
    expect(el.querySelector('[data-testid="runs-empty"]')).toBeNull();
  });

  function startOverButton(): HTMLButtonElement {
    return [...el.querySelectorAll('button')].find((b) => b.textContent!.trim() === 'Start over') as HTMLButtonElement;
  }

  async function openConfirm(): Promise<HTMLElement> {
    startOverButton().click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
    const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
    return containers[containers.length - 1];
  }

  function dialogButton(container: HTMLElement, label: string): HTMLButtonElement {
    return [...container.querySelectorAll('button')].find((b) => b.textContent!.trim() === label) as HTMLButtonElement;
  }

  it('Start over asks first, in a named alert dialog, then clears the state and reloads it', async () => {
    await mount();
    const container = await openConfirm();
    expectNamedDialog(container, { name: 'Start over?', role: 'alertdialog' });
    expect(api.clearWatchState).not.toHaveBeenCalled();

    api.watchState.mockReturnValue(of({ state: null }));
    dialogButton(container, 'Start over').click();
    await settle();

    expect(api.clearWatchState).toHaveBeenCalledWith('sched-1');
    expect(api.watchState).toHaveBeenCalledTimes(2);
    expect(toast.success).toHaveBeenCalled();
    expect(text('[data-testid="state-empty"]')).toContain('Nothing yet');
  });

  it('Start over does nothing when the confirmation is cancelled', async () => {
    await mount();
    const container = await openConfirm();
    dialogButton(container, 'Cancel').click();
    await settle();
    expect(api.clearWatchState).not.toHaveBeenCalled();
    expect(api.watchState).toHaveBeenCalledTimes(1);
  });

  it('a failed Start over says so and keeps the state shown', async () => {
    await mount();
    api.clearWatchState.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    const container = await openConfirm();
    dialogButton(container, 'Start over').click();
    await settle();
    expect(toast.error).toHaveBeenCalled();
    expect(el.querySelector('[data-testid="tracking-list"]')).not.toBeNull();
  });

  it('tells a project schedule’s runs are on the project, and asks for none', async () => {
    await mount({ schedule: stubSchedule({ projectId: 'prj_1' }) });
    expect(el.textContent).toContain('This schedule runs in a project.');
    expect(api.runs).not.toHaveBeenCalled();
    expect(api.watchState).not.toHaveBeenCalled();
  });
});

describe('compactTokens', () => {
  it('shortens thousands and millions', () => {
    expect(compactTokens(950)).toBe('950');
    expect(compactTokens(2000)).toBe('2k');
    expect(compactTokens(12_340)).toBe('12.3k');
    expect(compactTokens(1_200_000)).toBe('1.2M');
    expect(compactTokens(0)).toBe('0');
  });
});
