import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { NEVER, of, throwError } from 'rxjs';
import { signal } from '@angular/core';
import { ScheduleDetailPage, compactTokens, runTokens, watchHold } from './schedule-detail.page';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { AppNotification } from '../../services/notifications/notification.model';
import { InboxAnswerService } from '../services/inbox-answer.service';
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
  const inboxAnswer = { review: vi.fn().mockResolvedValue(undefined), settledCount: signal(0) };

  async function mount(opts: {
    schedule?: ScheduledPrompt | undefined;
    scheduleError?: unknown;
    runs?: ScheduleRun[];
    state?: WatchState | null;
    runsPending?: boolean;
    inbox?: AppNotification[];
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
        { provide: NotificationsService, useValue: { notifications: signal(opts.inbox ?? []) } },
        { provide: InboxAnswerService, useValue: inboxAnswer },
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
    expect(rows[0].textContent).toContain('Tokens: 40.5k in · 120 out');
    expect(rows[0].querySelector('a')!.getAttribute('href')).toBe('/s/sess-9');
    // A quiet run has no conversation to open.
    expect(rows[2].querySelector('a')).toBeNull();
  });

  it('sums up the last 24 hours above the list', async () => {
    await mount();
    expect(text('[data-testid="day-summary"]')).toBe('Last 24 hours: Checked 2 times · found something 1 time · 1 failed');
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

  describe('a run that waits on the user (answer-from-inbox §3.8)', () => {
    const PAUSE_UNTIL = new Date(NOW + 6 * 86_400_000).toISOString();
    const waitingRow = (over: Partial<AppNotification> = {}): AppNotification => ({
      notificationId: 'n1', recipientEmail: 'me@x.edu', kind: 'agent_needs_approval',
      payload: { scheduleId: 'sched-1', sessionId: 'headless-7', label: 'GitHub watch', expiresAt: PAUSE_UNTIL, answerable: true, prompts: [] },
      createdAt: ago(20), readAt: null, ...over,
    });
    const held: WatchState = { ...STATE, pausedSessionId: 'headless-7', pausedUntil: PAUSE_UNTIL };
    const pausedRuns = [
      run({ runId: 'r-wait', status: 'needs_user', sessionId: 'headless-7', finishedAt: ago(20), expiresAt: PAUSE_UNTIL }),
      run({ runId: 'r-done', status: 'needs_user', sessionId: 'headless-1', finishedAt: ago(90), resolution: 'approved', resumeOutcome: 'finished' }),
    ];

    it('labels each paused run by what became of it, and adds the waiting tail', async () => {
      await mount({ runs: pausedRuns, state: held });
      const labels = Array.from(el.querySelectorAll('[data-testid="run-list"] li > span')).map((s) => s.textContent?.trim());
      expect(labels[0]).toMatch(/^Waiting for you · expires /);
      expect(labels[1]).toBe('Approved · finished');
      expect(text('[data-testid="day-summary"]')).toBe('Last 24 hours: Checked 2 times · found something 2 times · 1 waiting for you');
    });

    it('a held watcher shows the banner, and Review opens the same dialog as the bell', async () => {
      const row = waitingRow();
      await mount({ runs: pausedRuns, state: held, inbox: [row] });
      const banner = text('[data-testid="watch-hold"]')!;
      expect(banner).toMatch(/^Waiting for you since .+\. Checks resume once you answer, or on .+ when it expires\. Review$/);
      (el.querySelector('[data-testid="watch-hold"] button') as HTMLButtonElement).click();
      expect(inboxAnswer.review).toHaveBeenCalledWith(row);
    });

    it('without a row the dialog can answer, the banner links to the conversation', async () => {
      await mount({ runs: pausedRuns, state: held, inbox: [] });
      const link = el.querySelector('[data-testid="watch-hold"] a');
      expect(link?.getAttribute('href')).toBe('/s/headless-7');
    });

    it('a watcher that isn’t holding shows no banner', async () => {
      await mount({ runs: pausedRuns, state: STATE });
      expect(el.querySelector('[data-testid="watch-hold"]')).toBeNull();
    });
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

describe('runTokens', () => {
  const base: ScheduleRun = {
    runId: 'r', scheduleId: 's', status: 'notify', itemCount: 1, finishedAt: '2026-11-25T00:00:00Z',
    inputTokens: 8, outputTokens: 180, cacheReadTokens: 4325, cacheWriteTokens: 4494,
  };

  it('names the cache write, which is most of a short run’s cost', () => {
    // A watcher run measured on dev: two calls, the prefix written once and read once.
    expect(runTokens(base)).toBe('Tokens: 8 in · 180 out · 4.5k written to cache · 4.3k read from cache');
  });

  it('reads a row from before cache writes were recorded', () => {
    const { cacheWriteTokens: _omit, ...old } = base;
    expect(runTokens(old)).toBe('Tokens: 8 in · 180 out · 4.3k read from cache');
  });

  it('says nothing for a run that stopped before it started', () => {
    expect(runTokens({ ...base, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 })).toBe('');
  });
});

describe('watchHold', () => {
  const now = Date.parse('2026-11-26T18:00:00Z');
  const until = '2026-12-02T18:00:00Z';
  const state: WatchState = { cursors: {}, tracking: [], seen: [], version: 1, pausedSessionId: 'headless-7', pausedUntil: until };
  const row = (over: Partial<AppNotification>): AppNotification => ({
    notificationId: 'n1', recipientEmail: 'me@x.edu', kind: 'agent_needs_input',
    payload: { sessionId: 'headless-7', expiresAt: until, answerable: true, prompts: [] },
    createdAt: '2026-11-26T17:00:00Z', readAt: null, ...over,
  });

  it('is null with no pause, or one past its expiry', () => {
    expect(watchHold(null, [], [], now)).toBeNull();
    expect(watchHold({ ...state, pausedUntil: '2026-11-26T17:59:00Z' }, [], [], now)).toBeNull();
    expect(watchHold({ ...state, pausedSessionId: null }, [], [], now)).toBeNull();
  });

  it('is null once the inbox shows the wait settled, ahead of the next tick clearing the state', () => {
    expect(watchHold(state, [], [row({ resolution: { state: 'answered_elsewhere', at: 'x' } })], now)).toBeNull();
  });

  it('offers the waiting row to review, and only an answerable one', () => {
    expect(watchHold(state, [], [row({})], now)?.notification?.notificationId).toBe('n1');
    expect(watchHold(state, [], [row({ payload: { sessionId: 'headless-7', expiresAt: until, answerable: false } })], now)?.notification).toBeNull();
    expect(watchHold(state, [], [], now)?.sentence).toMatch(/^Waiting for you\. Checks resume once you answer, or on Dec 2 when it expires\.$/);
  });
});
