import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { SchedulesPage } from './schedules.page';
import { ScheduleService } from './services/schedule.service';
import { GrantService } from './services/grant.service';
import { ToastService } from '../services/toast/toast.service';
import { ScheduledPrompt } from './models/schedule.model';
import { GrantStatus } from './models/grant.model';
import { ProjectsService } from '../projects/services/projects.service';
import { FEATURES } from '../services/features';

function stubSchedule(overrides: Partial<ScheduledPrompt> = {}): ScheduledPrompt {
  return {
    scheduleId: 'sched-abc123def456',
    assistantId: null,
    label: 'Morning Briefing',
    promptText: 'Summarize my classes',
    cadence: 'daily',
    hourLocal: 7,
    weekday: null,
    timezone: 'America/Boise',
    state: 'active',
    stateReason: null,
    nextRunAt: '2026-07-06T14:00:00Z',
    lastRunAt: null,
    lastRunStatus: null,
    lastRunSessionId: null,
    lastError: null,
    runsToday: 0,
    maxRunsPerDay: 24,
    enabledTools: null,
    deliverEmail: false,
    createdAt: '2026-07-05T00:00:00Z',
    updatedAt: '2026-07-05T00:00:00Z',
    ...overrides,
  };
}

function projectsStub(projects: Array<{ projectId: string; name: string }> = []) {
  return {
    projects$: signal(projects),
    available$: signal<boolean | null>(true),
    loading$: signal(false),
    load: vi.fn().mockResolvedValue(undefined),
  };
}

describe('SchedulesPage', () => {
  let mockScheduleService: {
    schedules$: ReturnType<typeof signal<ScheduledPrompt[]>>;
    loading$: ReturnType<typeof signal<boolean>>;
    accessible$: ReturnType<typeof signal<boolean | null>>;
    loadSchedules: ReturnType<typeof vi.fn>;
    pauseSchedule: ReturnType<typeof vi.fn>;
    resumeSchedule: ReturnType<typeof vi.fn>;
    deleteSchedule: ReturnType<typeof vi.fn>;
  };
  let mockGrantService: {
    status$: ReturnType<typeof signal<GrantStatus>>;
    loading$: ReturnType<typeof signal<boolean>>;
    loadStatus: ReturnType<typeof vi.fn>;
    enable: ReturnType<typeof vi.fn>;
  };
  let mockToast: { success: ReturnType<typeof vi.fn>; error: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockScheduleService = {
      schedules$: signal<ScheduledPrompt[]>([]),
      loading$: signal(false),
      accessible$: signal<boolean | null>(true),
      loadSchedules: vi.fn().mockResolvedValue(undefined),
      pauseSchedule: vi.fn().mockResolvedValue(undefined),
      resumeSchedule: vi.fn().mockResolvedValue(undefined),
      deleteSchedule: vi.fn().mockResolvedValue(undefined),
    };
    mockGrantService = {
      status$: signal<GrantStatus>({ enabled: false }),
      loading$: signal(false),
      loadStatus: vi.fn().mockResolvedValue(undefined),
      enable: vi.fn().mockResolvedValue({ enabled: true }),
    };
    mockToast = { success: vi.fn(), error: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        // Register the routes the component navigates to (new / edit); an
        // empty provideRouter([]) would make those navigations reject with
        // NG04002 as unhandled rejections and fail the vitest process.
        provideRouter([
          { path: 'schedules/new', children: [] },
          { path: 'schedules/:scheduleId/edit', children: [] },
        ]),
        { provide: ScheduleService, useValue: mockScheduleService },
        { provide: GrantService, useValue: mockGrantService },
        { provide: ToastService, useValue: mockToast },
        { provide: ProjectsService, useValue: projectsStub() },
        { provide: FEATURES, useValue: { projects: true } },
      ],
    });
    TestBed.overrideComponent(SchedulesPage, { set: { template: '<div></div>' } });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(SchedulesPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('loads schedules and grant status on init', () => {
    createComponent();
    expect(mockScheduleService.loadSchedules).toHaveBeenCalled();
    expect(mockGrantService.loadStatus).toHaveBeenCalled();
  });

  it('needsEnablement is true when the grant is disabled', () => {
    mockGrantService.status$.set({ enabled: false });
    const component = createComponent();
    expect(component.needsEnablement()).toBe(true);
  });

  it('needsEnablement is false when the grant is active', () => {
    mockGrantService.status$.set({ enabled: true, grantId: 'hlg-1' });
    const component = createComponent();
    expect(component.needsEnablement()).toBe(false);
  });

  it('onEnableScheduledRuns calls grant.enable() and toasts success', async () => {
    const component = createComponent();
    await component.onEnableScheduledRuns();
    expect(mockGrantService.enable).toHaveBeenCalled();
    expect(mockToast.success).toHaveBeenCalled();
  });

  it('onEnableScheduledRuns toasts an error on failure', async () => {
    mockGrantService.enable.mockRejectedValueOnce(new Error('409'));
    const component = createComponent();
    await component.onEnableScheduledRuns();
    expect(mockToast.error).toHaveBeenCalled();
  });

  it('needsReauth is true only for paused_error with reason reauth_required', () => {
    const component = createComponent();
    expect(
      component.needsReauth(stubSchedule({ state: 'paused_error', stateReason: 'reauth_required' })),
    ).toBe(true);
    expect(
      component.needsReauth(stubSchedule({ state: 'paused_error', stateReason: 'oauth_required' })),
    ).toBe(false);
    expect(component.needsReauth(stubSchedule({ state: 'active' }))).toBe(false);
  });

  it('needsOAuthReconnect is true only for paused_error with reason oauth_required', () => {
    const component = createComponent();
    expect(
      component.needsOAuthReconnect(
        stubSchedule({ state: 'paused_error', stateReason: 'oauth_required' }),
      ),
    ).toBe(true);
    expect(
      component.needsOAuthReconnect(
        stubSchedule({ state: 'paused_error', stateReason: 'reauth_required' }),
      ),
    ).toBe(false);
  });

  it('onReauthenticate re-enables the grant then resumes the schedule', async () => {
    const component = createComponent();
    const schedule = stubSchedule({ state: 'paused_error', stateReason: 'reauth_required' });

    await component.onReauthenticate(schedule);

    expect(mockGrantService.enable).toHaveBeenCalled();
    expect(mockScheduleService.resumeSchedule).toHaveBeenCalledWith(schedule.scheduleId);
    expect(mockToast.success).toHaveBeenCalled();
  });

  it('onReauthenticate leaves a project schedule for its project to resume', async () => {
    const component = createComponent();
    await component.onReauthenticate(stubSchedule({ state: 'paused_error', stateReason: 'reauth_required', projectId: 'prj_1' }));
    expect(mockGrantService.enable).toHaveBeenCalled();
    expect(mockScheduleService.resumeSchedule).not.toHaveBeenCalled();
    expect(mockToast.success).toHaveBeenCalledWith('Access renewed', 'Resume "Morning Briefing" from a project.');
  });

  it('onPause pauses the schedule', async () => {
    const component = createComponent();
    const schedule = stubSchedule();
    await component.onPause(schedule);
    expect(mockScheduleService.pauseSchedule).toHaveBeenCalledWith(schedule.scheduleId);
  });

  it('onResume resumes the schedule', async () => {
    const component = createComponent();
    const schedule = stubSchedule({ state: 'paused' });
    await component.onResume(schedule);
    expect(mockScheduleService.resumeSchedule).toHaveBeenCalledWith(schedule.scheduleId);
  });

  it('onDelete does nothing when the confirm dialog is cancelled', async () => {
    const component = createComponent();
    const dialog = TestBed.inject(Dialog);
    vi.spyOn(dialog, 'open').mockReturnValue({ closed: of(false) } as ReturnType<Dialog['open']>);

    await component.onDelete(stubSchedule());

    expect(mockScheduleService.deleteSchedule).not.toHaveBeenCalled();
  });

  it('onDelete deletes the schedule when confirmed', async () => {
    const component = createComponent();
    const dialog = TestBed.inject(Dialog);
    vi.spyOn(dialog, 'open').mockReturnValue({ closed: of(true) } as ReturnType<Dialog['open']>);

    const schedule = stubSchedule();
    await component.onDelete(schedule);

    expect(mockScheduleService.deleteSchedule).toHaveBeenCalledWith(schedule.scheduleId);
    expect(mockToast.success).toHaveBeenCalled();
  });

  it('cadenceSummary formats daily/weekday/weekly cadences', () => {
    const component = createComponent();
    expect(component.cadenceSummary(stubSchedule({ cadence: 'daily', hourLocal: 7 }))).toBe(
      'Every day at 7:00 AM',
    );
    expect(component.cadenceSummary(stubSchedule({ cadence: 'weekday', hourLocal: 13 }))).toBe(
      'Every weekday at 1:00 PM',
    );
    expect(
      component.cadenceSummary(stubSchedule({ cadence: 'weekly', hourLocal: 0, weekday: 0 })),
    ).toBe('Every Monday at 12:00 AM');
  });

  it('stateLabel maps states to display text', () => {
    const component = createComponent();
    expect(component.stateLabel(stubSchedule({ state: 'active' }))).toBe('Active');
    expect(component.stateLabel(stubSchedule({ state: 'paused' }))).toBe('Paused');
    expect(component.stateLabel(stubSchedule({ state: 'paused_error' }))).toBe('Needs attention');
  });
});

describe('SchedulesPage with a project schedule', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        {
          provide: ScheduleService,
          useValue: {
            schedules$: signal<ScheduledPrompt[]>([
              stubSchedule({ scheduleId: 'p1', label: 'Weekly digest', state: 'paused', projectId: 'prj_1', deliverTo: 'project' }),
              stubSchedule({ scheduleId: 'm1', label: 'Mine', state: 'paused' }),
            ]),
            loading$: signal(false),
            accessible$: signal<boolean | null>(true),
            loadSchedules: vi.fn().mockResolvedValue(undefined),
          },
        },
        {
          provide: GrantService,
          useValue: { status$: signal<GrantStatus>({ enabled: true }), loading$: signal(false), loadStatus: vi.fn().mockResolvedValue(undefined) },
        },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
        { provide: ProjectsService, useValue: projectsStub([{ projectId: 'prj_1', name: 'Enrollment Sync' }]) },
        { provide: FEATURES, useValue: { projects: true } },
      ],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(SchedulesPage);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const item = (label: string) => Array.from(el.querySelectorAll('li')).find(li => li.textContent?.includes(label))!;
    return { el, item };
  }

  it('badges it with its project and sends changes there instead of offering Edit or Resume', () => {
    const { item } = render();
    const project = item('Weekly digest');
    const badge = project.querySelector<HTMLAnchorElement>('[data-testid="project-badge"]')!;
    expect(badge.textContent?.trim()).toBe('In Enrollment Sync');
    expect(badge.getAttribute('href')).toBe('/projects/prj_1/schedules');
    const actions = Array.from(project.querySelectorAll('button, a')).map(b => b.textContent?.trim());
    expect(actions).toContain('Manage in project');
    expect(actions).toContain('Delete');
    expect(actions).not.toContain('Edit');
    expect(actions).not.toContain('Resume');

    const mine = Array.from(item('Mine').querySelectorAll('button')).map(b => b.textContent?.trim());
    expect(mine).toEqual(expect.arrayContaining(['Resume', 'Edit', 'Delete']));
    expect(item('Mine').querySelector('[data-testid="project-badge"]')).toBeNull();
  });

  it('names an unknown project neutrally', () => {
    TestBed.overrideProvider(ProjectsService, { useValue: projectsStub() });
    const { item } = render();
    expect(item('Weekly digest').querySelector('[data-testid="project-badge"]')?.textContent?.trim()).toBe('In a project');
  });
});
