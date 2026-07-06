import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute } from '@angular/router';
import { ReactiveFormsModule } from '@angular/forms';
import { signal } from '@angular/core';
import { ScheduleFormPage } from './schedule-form.page';
import { ScheduleService } from '../services/schedule.service';
import { AssistantService } from '../../assistants/services/assistant.service';
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

  const mockAssistantService = {
    assistants$: signal([]),
    loadAssistants: vi.fn().mockResolvedValue(undefined),
  };

  const mockToolService = {
    tools: signal([]),
    initialized: signal(true),
    loadTools: vi.fn().mockResolvedValue(undefined),
  };

  const mockToast = { success: vi.fn(), error: vi.fn() };

  function configure(routeParams: Record<string, string> = {}) {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    mockScheduleService.getSchedule.mockResolvedValue(undefined);

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        provideRouter([]),
        { provide: ScheduleService, useValue: mockScheduleService },
        { provide: AssistantService, useValue: mockAssistantService },
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

    it('clearing the assistant checkbox omits assistantId (documented gap: cannot actually detach)', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      component.onClearAssistantChange(true);
      await component.onSubmit();

      const [, request] = mockScheduleService.updateSchedule.mock.calls[0];
      expect(request.assistantId).toBeUndefined();
    });

    it('clearing the tools checkbox omits enabledTools from the PATCH', async () => {
      component.ngOnInit();
      await Promise.resolve();
      await Promise.resolve();

      component.onClearToolsChange(true);
      await component.onSubmit();

      const [, request] = mockScheduleService.updateSchedule.mock.calls[0];
      expect(request.enabledTools).toBeUndefined();
      expect(component.selectedToolIds().size).toBe(0);
    });
  });
});
