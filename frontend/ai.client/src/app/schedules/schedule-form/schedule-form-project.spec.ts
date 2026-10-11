import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute, Router } from '@angular/router';
import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ScheduleFormPage, changedFields } from './schedule-form.page';
import { ScheduleService } from '../services/schedule.service';
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { ToolService } from '../../services/tool/tool.service';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectApiService } from '../../projects/services/project-api.service';
import { Project, ProjectSchedule } from '../../projects/models/project.model';

/**
 * The schedule form's project mode (`?projectId=…`, shared-projects 3.2c): the
 * project's assistant and tools decide what runs, so the agent and tool pickers
 * give way to delivery; it saves through the project's routes and returns to the
 * project with its Schedules dialog open.
 */

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 2,
  harnessAgentId: 'ast-harness',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

function schedule(over: Partial<ProjectSchedule> = {}): ProjectSchedule {
  return {
    scheduleId: 'sc_1',
    label: 'Weekly digest',
    promptText: 'Summarize the week.',
    cadence: 'weekly',
    hourLocal: 9,
    weekday: 1,
    intervalValue: null,
    intervalUnit: null,
    timezone: 'America/Boise',
    deliverTo: 'project',
    notifyMembers: false,
    state: 'active',
    createdByEmail: 'me@x.edu',
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    isMine: true,
    canEdit: true,
    canManage: true,
    ...over,
  };
}

describe('ScheduleFormPage — project mode', () => {
  let fixture: ComponentFixture<ScheduleFormPage>;
  let component: ScheduleFormPage;
  let el: HTMLElement;
  const api = { get: vi.fn(), schedule: vi.fn(), createSchedule: vi.fn(), updateSchedule: vi.fn() };
  const scheduleService = { getSchedule: vi.fn(), createSchedule: vi.fn(), updateSchedule: vi.fn() };
  const agents = { agents$: signal([]), loadAgents: vi.fn() };
  const tools = { tools: signal([]), initialized: signal(false), loadTools: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
  const runNow = { run: vi.fn() };

  async function mount(scheduleId: string | null = null, project: Project = PROJECT, loaded = schedule()): Promise<void> {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(project));
    api.schedule.mockReturnValue(of(loaded));
    api.createSchedule.mockReturnValue(of(schedule()));
    api.updateSchedule.mockReturnValue(of(schedule()));
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'projects/:id/:tab', children: [] }]),
        { provide: ProjectApiService, useValue: api },
        { provide: ScheduleService, useValue: scheduleService },
        { provide: RunNowService, useValue: runNow },
        { provide: AgentService, useValue: agents },
        { provide: ToolService, useValue: tools },
        { provide: ToastService, useValue: toast },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              paramMap: { get: (key: string) => (key === 'scheduleId' ? scheduleId : null) },
              queryParamMap: { get: (key: string) => (key === 'projectId' ? 'prj_1' : null) },
            },
          },
        },
      ],
    });
    fixture = TestBed.createComponent(ScheduleFormPage);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
    await fixture.whenStable();
    await new Promise(resolve => setTimeout(resolve, 0));
    fixture.detectChanges();
  }

  const fill = () =>
    component.form.patchValue({ label: 'Weekly digest', promptText: 'Summarize the week.', cadence: 'daily', hourLocal: 7 });

  afterEach(() => TestBed.resetTestingModule());

  it('offers delivery instead of the agent and tool pickers, and never loads them', async () => {
    await mount();
    expect(el.querySelector('#assistantId')).toBeNull();
    expect(el.querySelector('app-tool-selector')).toBeNull();
    expect(agents.loadAgents).not.toHaveBeenCalled();
    expect(tools.loadTools).not.toHaveBeenCalled();
    const share = el.querySelector<HTMLInputElement>('#deliverToProject')!;
    const notify = el.querySelector<HTMLInputElement>('#notifyMembers')!;
    expect(el.querySelector('label[for="deliverToProject"]')?.textContent).toContain('Share each run with the project');
    expect(el.querySelector('label[for="notifyMembers"]')?.textContent).toContain('Notify members when a run is shared');
    expect(notify.disabled).toBe(true);
    share.click();
    fixture.detectChanges();
    expect(notify.disabled).toBe(false);
    expect(el.textContent).toContain('Back to Enrollment Sync');
  });

  it('creates through the project, then goes back to its Schedules dialog', async () => {
    await mount();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    fill();
    component.form.controls.deliverToProject.setValue(true);
    component.form.controls.notifyMembers.setValue(true);
    await component.onSubmit();
    expect(scheduleService.createSchedule).not.toHaveBeenCalled();
    expect(api.createSchedule).toHaveBeenCalledWith('prj_1', {
      label: 'Weekly digest',
      promptText: 'Summarize the week.',
      cadence: 'daily',
      hourLocal: 7,
      weekday: null,
      intervalValue: null,
      intervalUnit: null,
      timezone: component.form.controls.timezone.value,
      deliverTo: 'project',
      notifyMembers: true,
    });
    expect(navigate).toHaveBeenCalledWith(['/projects', 'prj_1', 'schedules']);
  });

  it('never asks to notify members about runs it does not share', async () => {
    await mount();
    fill();
    component.form.controls.deliverToProject.setValue(true);
    component.form.controls.notifyMembers.setValue(true);
    component.form.controls.deliverToProject.setValue(false);
    await component.onSubmit();
    expect(api.createSchedule.mock.calls[0][1]).toMatchObject({ deliverTo: 'owner', notifyMembers: false });
  });

  it('shows the API’s refusal in the error toast', async () => {
    await mount();
    api.createSchedule.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'This project is archived. Restore it to make changes.' } })),
    );
    fill();
    await component.onSubmit();
    expect(toast.error).toHaveBeenCalledWith('Could not save schedule', 'This project is archived. Restore it to make changes.');
  });

  it('edits only the fields that changed', async () => {
    await mount('sc_1');
    expect(api.schedule).toHaveBeenCalledWith('prj_1', 'sc_1');
    expect(component.form.controls.label.value).toBe('Weekly digest');
    expect(component.form.controls.deliverToProject.value).toBe(true);
    component.form.controls.promptText.setValue('Summarize the week, briefly.');
    component.form.controls.notifyMembers.setValue(true);
    await component.onSubmit();
    expect(api.updateSchedule).toHaveBeenCalledWith('prj_1', 'sc_1', {
      promptText: 'Summarize the week, briefly.',
      notifyMembers: true,
    });
  });

  it('refuses to edit someone else’s schedule', async () => {
    await mount('sc_1', PROJECT, schedule({ isMine: false, canEdit: false }));
    expect(el.textContent).toContain('Only the person who made this schedule can change it.');
    expect(el.querySelector('form')).toBeNull();
  });

  it('refuses a viewer a new schedule', async () => {
    await mount(null, { ...PROJECT, role: 'viewer' });
    expect(el.textContent).toContain('Only editors and the owner can add schedules to this project.');
  });

  it('runs once on the project’s assistant', async () => {
    await mount();
    fill();
    component.runNow();
    expect(runNow.run).toHaveBeenCalledWith({
      prompt: 'Summarize the week.',
      title: 'Weekly digest',
      ragAssistantId: 'ast-harness',
      enabledTools: null,
    });
  });
});

describe('changedFields', () => {
  it('compares a schedule with an edit, treating absent and null alike', () => {
    expect(
      changedFields(schedule({ cadence: 'daily', weekday: undefined }), {
        label: 'Weekly digest',
        promptText: 'Summarize the week.',
        cadence: 'daily',
        hourLocal: 9,
        weekday: null,
        intervalValue: null,
        intervalUnit: null,
        timezone: 'America/Denver',
        deliverTo: 'owner',
        notifyMembers: false,
      }),
    ).toEqual({ timezone: 'America/Denver', deliverTo: 'owner' });
  });
});
