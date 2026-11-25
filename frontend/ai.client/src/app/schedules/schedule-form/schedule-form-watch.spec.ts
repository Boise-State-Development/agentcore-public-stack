import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute } from '@angular/router';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { ScheduleFormPage } from './schedule-form.page';
import { ScheduleService } from '../services/schedule.service';
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { ToolService } from '../../services/tool/tool.service';
import { ToastService } from '../../services/toast/toast.service';
import { FEATURES } from '../../services/features';
import { ModelService } from '../../session/services/model/model.service';
import { ProjectApiService } from '../../projects/services/project-api.service';
import { ScheduledPrompt } from '../models/schedule.model';

/**
 * The quiet-watcher fields on the schedule form (docs/specs/quiet-watcher.md §3.1):
 * "What should it do?" and a Model picker, shown and sent only while
 * `features.proactiveRuns` is on and only for a personal schedule, with a note
 * (never an error) for a watcher that checks more often than hourly.
 */

const MODELS = [
  { modelId: 'us.small', modelName: 'Small Model', providerName: 'Bedrock', status: 'active' },
  { modelId: 'us.big', modelName: 'Big Model', providerName: 'Bedrock', status: 'active' },
  { modelId: 'us.old', modelName: 'Old Model', providerName: 'Bedrock', status: 'deprecated' },
];

function stubSchedule(overrides: Partial<ScheduledPrompt> = {}): ScheduledPrompt {
  return {
    scheduleId: 'sched-1',
    assistantId: null,
    label: 'Inbox watch',
    promptText: 'Check my notifications',
    cadence: 'interval',
    hourLocal: 0,
    weekday: null,
    intervalValue: 30,
    intervalUnit: 'minutes',
    timezone: 'UTC',
    state: 'active',
    runsToday: 0,
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

describe('ScheduleFormPage — quiet-watcher fields', () => {
  let fixture: ComponentFixture<ScheduleFormPage>;
  let component: ScheduleFormPage;
  let el: HTMLElement;
  let scheduleService: {
    getSchedule: ReturnType<typeof vi.fn>;
    createSchedule: ReturnType<typeof vi.fn>;
    updateSchedule: ReturnType<typeof vi.fn>;
  };

  async function mount(opts: {
    proactiveRuns: boolean;
    scheduleId?: string | null;
    projectId?: string | null;
    loaded?: ScheduledPrompt;
  }): Promise<void> {
    TestBed.resetTestingModule();
    scheduleService = {
      getSchedule: vi.fn().mockResolvedValue(opts.loaded ?? stubSchedule()),
      createSchedule: vi.fn().mockResolvedValue(stubSchedule()),
      updateSchedule: vi.fn().mockResolvedValue(stubSchedule()),
    };
    const providers: unknown[] = [
      provideRouter([{ path: 'schedules', children: [] }, { path: 'projects/:id/:tab', children: [] }]),
      { provide: ScheduleService, useValue: scheduleService },
      { provide: RunNowService, useValue: { run: vi.fn(), running: signal(false) } },
      { provide: AgentService, useValue: { agents$: signal([]), loadAgents: vi.fn().mockResolvedValue(undefined) } },
      { provide: ToolService, useValue: { tools: signal([]), initialized: signal(true), loadTools: vi.fn() } },
      { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn(), info: vi.fn() } },
      { provide: FEATURES, useValue: { proactiveRuns: opts.proactiveRuns, projects: true } },
      {
        provide: ProjectApiService,
        useValue: {
          get: vi.fn().mockReturnValue(of({ projectId: 'prj_1', name: 'P', role: 'owner', status: 'active' })),
          schedule: vi.fn(),
        },
      },
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: {
            paramMap: { get: (key: string) => (key === 'scheduleId' ? (opts.scheduleId ?? null) : null) },
            queryParamMap: { get: (key: string) => (key === 'projectId' ? (opts.projectId ?? null) : null) },
          },
        },
      },
    ];
    // Provided only with the flag on: with it off, the form must not inject the
    // model list at all (an unprovided ModelService would need HttpClient and throw).
    if (opts.proactiveRuns) {
      providers.push({
        provide: ModelService,
        useValue: {
          availableModels: signal(MODELS),
          modelsLoading: signal(false),
          modelNameFor: (id: string) => MODELS.find((m) => m.modelId === id)?.modelName ?? null,
        },
      });
    }
    TestBed.configureTestingModule({ providers: providers as never[] });
    fixture = TestBed.createComponent(ScheduleFormPage);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function fillBasics(): void {
    component.form.patchValue({ label: 'Inbox watch', promptText: 'Check my notifications', timezone: 'UTC' });
  }

  function render(): void {
    fixture.detectChanges();
  }

  afterEach(() => TestBed.resetTestingModule());

  describe('with the flag off', () => {
    it('offers neither field', async () => {
      await mount({ proactiveRuns: false });
      expect(el.querySelector('input[name="mode"], #schedule-mode-watch')).toBeNull();
      expect(el.querySelector('#schedule-model')).toBeNull();
      expect(el.textContent).not.toContain('What should it do?');
    });

    it('sends exactly the create body it sent before watchers existed', async () => {
      await mount({ proactiveRuns: false });
      fillBasics();
      component.form.controls.mode.setValue('watch'); // even a stray value never leaks
      component.form.controls.modelId.setValue('us.small');
      await component.onSubmit();

      const [request] = scheduleService.createSchedule.mock.calls[0];
      expect(JSON.stringify(request)).toBe(
        JSON.stringify({
          label: 'Inbox watch',
          promptText: 'Check my notifications',
          cadence: 'daily',
          hourLocal: 7,
          weekday: null,
          intervalValue: null,
          intervalUnit: null,
          timezone: 'UTC',
          assistantId: null,
          enabledTools: null,
        }),
      );
    });

    it('sends no watcher field on an edit', async () => {
      await mount({ proactiveRuns: false, scheduleId: 'sched-1' });
      await component.onSubmit();
      const [, request] = scheduleService.updateSchedule.mock.calls[0];
      expect(Object.keys(request)).not.toContain('mode');
      expect(Object.keys(request)).not.toContain('modelId');
      expect(Object.keys(request)).not.toContain('clearModel');
    });

    it('shows no short-interval note', async () => {
      await mount({ proactiveRuns: false });
      component.form.controls.cadence.setValue('interval');
      component.form.patchValue({ intervalValue: 15, intervalUnit: 'minutes' });
      component.form.controls.mode.setValue('watch');
      render();
      expect(component.shortIntervalNote()).toBeNull();
      expect(el.querySelector('[data-testid="short-interval-note"]')).toBeNull();
    });
  });

  describe('with the flag on', () => {
    it('offers the mode choice, with help for each, and the model picker', async () => {
      await mount({ proactiveRuns: true });
      expect(el.textContent).toContain('What should it do?');
      const standard = el.querySelector('#schedule-mode-standard') as HTMLInputElement;
      const watch = el.querySelector('#schedule-mode-watch') as HTMLInputElement;
      expect(standard.checked).toBe(true);
      expect(el.querySelector('label[for="schedule-mode-standard"]')!.textContent).toContain('Run and deliver every time');
      expect(el.querySelector('label[for="schedule-mode-watch"]')!.textContent).toContain(
        'Watch, and only tell me when something is new',
      );
      expect(el.querySelector(`#${watch.getAttribute('aria-describedby')}`)?.textContent?.trim()).toBeTruthy();

      const select = el.querySelector('#schedule-model') as HTMLSelectElement;
      const options = [...select.options].map((o) => o.textContent!.trim());
      // Default first; a model being retired is not offered.
      expect(options).toEqual(['Default', 'Small Model (Bedrock)', 'Big Model (Bedrock)']);
    });

    it('creates a watcher with its model', async () => {
      await mount({ proactiveRuns: true });
      fillBasics();
      (el.querySelector('#schedule-mode-watch') as HTMLInputElement).click();
      component.form.controls.modelId.setValue('us.small');
      await component.onSubmit();
      const [request] = scheduleService.createSchedule.mock.calls[0];
      expect(request.mode).toBe('watch');
      expect(request.modelId).toBe('us.small');
    });

    it('leaves modelId out for the default model', async () => {
      await mount({ proactiveRuns: true });
      fillBasics();
      await component.onSubmit();
      const [request] = scheduleService.createSchedule.mock.calls[0];
      expect(request.mode).toBe('standard');
      expect('modelId' in request).toBe(false);
    });

    it('loads the schedule’s mode and model on edit', async () => {
      await mount({ proactiveRuns: true, scheduleId: 'sched-1' });
      expect(component.form.controls.mode.value).toBe('watch');
      expect(component.form.controls.modelId.value).toBe('us.small');
      expect((el.querySelector('#schedule-mode-watch') as HTMLInputElement).checked).toBe(true);
    });

    it('sends no model on an edit that leaves it alone', async () => {
      await mount({ proactiveRuns: true, scheduleId: 'sched-1' });
      await component.onSubmit();
      const [, request] = scheduleService.updateSchedule.mock.calls[0];
      expect(request.mode).toBe('watch');
      expect('modelId' in request).toBe(false);
      expect('clearModel' in request).toBe(false);
    });

    it('sends a changed model, and clearModel when it goes back to Default', async () => {
      await mount({ proactiveRuns: true, scheduleId: 'sched-1' });
      component.form.controls.modelId.setValue('us.big');
      await component.onSubmit();
      let [, request] = scheduleService.updateSchedule.mock.calls[0];
      expect(request.modelId).toBe('us.big');
      expect('clearModel' in request).toBe(false);

      component.form.controls.modelId.setValue(null);
      await component.onSubmit();
      [, request] = scheduleService.updateSchedule.mock.calls[1];
      expect(request.clearModel).toBe(true);
      expect('modelId' in request).toBe(false);
    });

    it('keeps a saved model the list no longer has as a choice', async () => {
      await mount({ proactiveRuns: true, scheduleId: 'sched-1', loaded: stubSchedule({ modelId: 'us.gone' }) });
      expect(component.modelChoices()[0]).toEqual({ modelId: 'us.gone', label: 'us.gone' });
    });

    it('notes the runs per day for a watcher under an hour, as a note and never an error', async () => {
      await mount({ proactiveRuns: true });
      fillBasics();
      component.form.controls.mode.setValue('watch');
      component.form.controls.cadence.setValue('interval');
      component.form.patchValue({ intervalValue: 30, intervalUnit: 'minutes' });
      render();
      const note = el.querySelector('[data-testid="short-interval-note"]');
      expect(note?.textContent).toContain('about 48 times a day');
      expect(note?.textContent).toContain('small, inexpensive model');
      expect(note?.getAttribute('role')).toBeNull();

      await component.onSubmit();
      expect(scheduleService.createSchedule).toHaveBeenCalled();
    });

    it('has no note at an hour or more, for a standard schedule, or off an interval', async () => {
      await mount({ proactiveRuns: true });
      component.form.controls.cadence.setValue('interval');
      component.form.patchValue({ intervalValue: 1, intervalUnit: 'hours' });
      component.form.controls.mode.setValue('watch');
      expect(component.shortIntervalNote()).toBeNull();

      component.form.patchValue({ intervalValue: 45, intervalUnit: 'minutes' });
      expect(component.shortIntervalNote()).toContain('about 32 times a day');

      component.form.controls.mode.setValue('standard');
      expect(component.shortIntervalNote()).toBeNull();

      component.form.controls.mode.setValue('watch');
      component.form.controls.cadence.setValue('daily');
      expect(component.shortIntervalNote()).toBeNull();
    });

    it('offers neither field on a project schedule, which can’t watch', async () => {
      await mount({ proactiveRuns: true, projectId: 'prj_1' });
      expect(component.offersWatchFields()).toBe(false);
      expect(el.querySelector('#schedule-model')).toBeNull();
      expect(el.textContent).not.toContain('What should it do?');
    });
  });
});
