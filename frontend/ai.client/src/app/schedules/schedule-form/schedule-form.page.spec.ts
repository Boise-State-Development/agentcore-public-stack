import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute, Router } from '@angular/router';
import { ReactiveFormsModule } from '@angular/forms';
import { signal } from '@angular/core';
import { ScheduleFormPage } from './schedule-form.page';
import { ScheduleService } from '../services/schedule.service';
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { ToolService } from '../../services/tool/tool.service';
import { ToastService } from '../../services/toast/toast.service';
import { ScheduledPrompt } from '../models/schedule.model';

function stubSchedule(overrides: Partial<ScheduledPrompt> = {}): ScheduledPrompt {
  return {
    scheduleId: 'sched-abc123def456',
    assistantId: 'ast-1',
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
    enabledTools: ['class_search'],
    deliverEmail: false,
    createdAt: '2026-07-05T00:00:00Z',
    updatedAt: '2026-07-05T00:00:00Z',
    ...overrides,
  };
}

describe('ScheduleFormPage', () => {
  let component: ScheduleFormPage;
  let fixture: ComponentFixture<ScheduleFormPage>;

  const mockScheduleService = {
    getSchedule: vi.fn().mockResolvedValue(undefined),
    createSchedule: vi.fn().mockResolvedValue(stubSchedule()),
    updateSchedule: vi.fn().mockResolvedValue(stubSchedule()),
  };

  const mockAgentService = {
    agents$: signal([]),
    loadAgents: vi.fn().mockResolvedValue(undefined),
  };

  const mockToolService = {
    tools: signal([]),
    initialized: signal(true),
    loadTools: vi.fn().mockResolvedValue(undefined),
  };

  const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn() };

  const mockRunNow = {
    run: vi.fn().mockReturnValue('bgtask-1'),
  };

  function configure(routeParams: Record<string, string> = {}) {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    mockScheduleService.getSchedule.mockResolvedValue(undefined);
    mockRunNow.run.mockReturnValue('bgtask-1');

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        // Register the route the component navigates to on save/cancel
        // (`router.navigate(['/schedules'])`); an empty `provideRouter([])`
        // makes that navigation reject with NG04002 as an unhandled
        // rejection after the test body, failing the whole vitest process.
        provideRouter([{ path: 'schedules', children: [] }]),
        { provide: ScheduleService, useValue: mockScheduleService },
        { provide: RunNowService, useValue: mockRunNow },
        { provide: AgentService, useValue: mockAgentService },
        { provide: ToolService, useValue: mockToolService },
        { provide: ToastService, useValue: mockToast },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: (key: string) => routeParams[key] ?? null } } },
        },
      ],
    })
      .overrideComponent(ScheduleFormPage, { set: { template: '<div></div>' } })
      .compileComponents();

    fixture = TestBed.createComponent(ScheduleFormPage);
    component = fixture.componentInstance;
  }

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('create mode', () => {
    beforeEach(() => {
      configure();
      component.ngOnInit();
    });

    it('is in create mode with no scheduleId', () => {
      expect(component.mode()).toBe('create');
      expect(component.scheduleId()).toBeNull();
    });

    it('defaults weekday to Monday the first time weekly is chosen', () => {
      expect(component.form.controls.weekday.value).toBeNull();
      component.form.controls.cadence.setValue('weekly');
      expect(component.form.controls.weekday.value).toBe(1);
    });

    it('rejects submit when required fields are missing', async () => {
      await component.onSubmit();
      expect(mockScheduleService.createSchedule).not.toHaveBeenCalled();
    });

    it('creates a schedule with the selected tools', async () => {
      component.form.patchValue({
        label: 'Test',
        promptText: 'Do the thing',
        cadence: 'daily',
        hourLocal: 8,
        timezone: 'UTC',
      });
      component.toggleTool('class_search');

      await component.onSubmit();

      expect(mockScheduleService.createSchedule).toHaveBeenCalledWith(
        expect.objectContaining({
          label: 'Test',
          promptText: 'Do the thing',
          cadence: 'daily',
          hourLocal: 8,
          timezone: 'UTC',
          enabledTools: ['class_search'],
        }),
      );
    });

    it('drops the manual tool snapshot when an agent is selected', async () => {
      component.form.patchValue({
        label: 'Test',
        promptText: 'Do the thing',
        cadence: 'daily',
        hourLocal: 8,
        timezone: 'UTC',
      });
      // Pick some tools first, then target an agent — the agent's bound tools
      // govern the run, so the snapshot must not be sent.
      component.toggleTool('class_search');
      component.form.controls.assistantId.setValue('ast-9');

      expect(component.agentSelected()).toBe(true);
      await component.onSubmit();

      expect(mockScheduleService.createSchedule).toHaveBeenCalledWith(
        expect.objectContaining({ assistantId: 'ast-9', enabledTools: null }),
      );
    });

    it('run now targets the selected agent and omits the tool snapshot', () => {
      component.form.patchValue({ label: 'Test', promptText: 'Do the thing' });
      component.toggleTool('class_search');
      component.form.controls.assistantId.setValue('ast-9');

      component.runNow();

      expect(mockRunNow.run).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt: 'Do the thing',
          ragAssistantId: 'ast-9',
          enabledTools: null,
        }),
      );
    });

    it('requires a weekday when cadence is weekly', async () => {
      component.form.patchValue({
        label: 'Test',
        promptText: 'Do the thing',
        cadence: 'weekly',
        hourLocal: 8,
        timezone: 'UTC',
        weekday: null,
      });

      await component.onSubmit();

      expect(mockScheduleService.createSchedule).not.toHaveBeenCalled();
      expect(component.form.controls.weekday.errors).toEqual({ required: true });
    });

    it('defaults the interval to every 6 hours the first time interval is chosen', () => {
      expect(component.isInterval()).toBe(false);
      component.form.controls.cadence.setValue('interval');
      expect(component.isInterval()).toBe(true);
      expect(component.form.controls.intervalValue.value).toBe(6);
      expect(component.form.controls.intervalUnit.value).toBe('hours');
    });

    it('creates an interval schedule with value and unit', async () => {
      component.form.patchValue({
        label: 'Every 6h',
        promptText: 'Check in',
        cadence: 'interval',
        intervalValue: 6,
        intervalUnit: 'hours',
      });

      await component.onSubmit();

      expect(mockScheduleService.createSchedule).toHaveBeenCalledWith(
        expect.objectContaining({
          cadence: 'interval',
          intervalValue: 6,
          intervalUnit: 'hours',
        }),
      );
    });

    it('rejects an interval below the minimum floor', async () => {
      component.form.patchValue({
        label: 'Too frequent',
        promptText: 'Check in',
        cadence: 'interval',
        intervalValue: 5,
        intervalUnit: 'minutes',
      });

      await component.onSubmit();

      expect(mockScheduleService.createSchedule).not.toHaveBeenCalled();
      expect(component.form.controls.intervalValue.errors).toEqual({ min: true });
    });

    it('run now hands the prompt to the background runner without saving or navigating', () => {
      const router = TestBed.inject(Router);
      const navigateSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
      component.form.patchValue({ label: 'Test', promptText: 'Do the thing' });
      component.toggleTool('class_search');

      component.runNow();

      expect(mockRunNow.run).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt: 'Do the thing',
          title: 'Test',
          enabledTools: ['class_search'],
        }),
      );
      expect(mockScheduleService.createSchedule).not.toHaveBeenCalled();
      // The view must not change — tracking happens in the corner toast.
      expect(navigateSpy).not.toHaveBeenCalled();
    });

    it('run now requires a prompt', () => {
      component.runNow();
      expect(mockRunNow.run).not.toHaveBeenCalled();
      expect(component.form.controls.promptText.errors).toEqual({ required: true });
    });
  });

  describe('edit mode', () => {
    beforeEach(() => {
      configure({ scheduleId: 'sched-abc123def456' });
      mockScheduleService.getSchedule.mockResolvedValue(stubSchedule());
    });

    it('loads the existing schedule into the form', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      expect(component.form.controls.label.value).toBe('Morning Briefing');
      expect(component.form.controls.assistantId.value).toBe('ast-1');
      expect(component.selectedToolIds()).toEqual(new Set(['class_search']));
    });

    it('omits assistantId from the PATCH when unchanged and non-empty', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      await component.onSubmit();

      const [, request] = mockScheduleService.updateSchedule.mock.calls[0];
      expect(request.assistantId).toBe('ast-1');
    });

    it('clearing the assistant checkbox sends clearAssistant instead of assistantId', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      component.onClearAssistantChange(true);
      await component.onSubmit();

      const [, request] = mockScheduleService.updateSchedule.mock.calls[0];
      expect(request.clearAssistant).toBe(true);
      expect(request.assistantId).toBeUndefined();
    });

    it('clearing the tools checkbox sends clearTools instead of enabledTools', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      component.onClearToolsChange(true);
      await component.onSubmit();

      const [, request] = mockScheduleService.updateSchedule.mock.calls[0];
      expect(request.clearTools).toBe(true);
      expect(request.enabledTools).toBeUndefined();
      expect(component.selectedToolIds().size).toBe(0);
    });
  });
});
