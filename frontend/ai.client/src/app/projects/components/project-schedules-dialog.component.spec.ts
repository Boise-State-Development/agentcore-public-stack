import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { Router, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import {
  ProjectSchedulesDialogComponent,
  ProjectSchedulesDialogData,
} from './project-schedules-dialog.component';
import { ConfirmationDialogComponent } from '../../components/confirmation-dialog/confirmation-dialog.component';
import { ProjectApiService } from '../services/project-api.service';
import { GrantService } from '../../schedules/services/grant.service';
import { GrantStatus } from '../../schedules/models/grant.model';
import { Project, ProjectSchedule } from '../models/project.model';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';

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
  harnessAgentId: 'ast-1',
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
    timezone: 'America/Boise',
    deliverTo: 'project',
    notifyMembers: true,
    publishOutputs: false,
    state: 'active',
    stateReason: null,
    nextRunAt: null,
    lastRunAt: null,
    createdByEmail: 'me@x.edu',
    createdByName: null,
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    isMine: true,
    canEdit: true,
    canManage: true,
    ...over,
  };
}

const THEIRS = schedule({
  scheduleId: 'sc_2',
  label: 'Roster check',
  cadence: 'interval',
  intervalValue: 6,
  intervalUnit: 'hours',
  deliverTo: 'owner',
  notifyMembers: false,
  state: 'paused_error',
  stateReason: 'member_removed',
  createdByEmail: 'bo@x.edu',
  createdByName: 'Bo Diaz',
  isMine: false,
  canEdit: false,
  canManage: true,
});

describe('ProjectSchedulesDialogComponent', () => {
  const api = {
    schedules: vi.fn(),
    pauseSchedule: vi.fn(),
    resumeSchedule: vi.fn(),
    deleteSchedule: vi.fn(),
    scheduleRuns: vi.fn(),
  };
  const grants = {
    status$: signal<GrantStatus>({ enabled: true }),
    loading$: signal(false),
    loadStatus: vi.fn(),
    enable: vi.fn(),
  };
  let changes: Array<ProjectSchedule[] | null>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    changes = [];
    grants.status$.set({ enabled: true });
    grants.loadStatus.mockResolvedValue(undefined);
    api.schedules.mockReturnValue(of({ schedules: [schedule(), THEIRS] }));
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: GrantService, useValue: grants },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(project: Project = PROJECT) {
    const opened = await openInCdkDialog<ProjectSchedulesDialogComponent, ProjectSchedulesDialogData>(
      ProjectSchedulesDialogComponent,
      { data: { project: signal(project), onSchedulesChange: list => changes.push(list) } },
    );
    await new Promise(resolve => setTimeout(resolve, 0));
    const container = opened.container;
    const item = (label: string) =>
      Array.from(container.querySelectorAll<HTMLLIElement>('li[data-schedule]')).find(li => li.textContent?.includes(label))!;
    const buttons = (node: Element) => Array.from(node.querySelectorAll('button')).map(b => b.textContent?.trim());
    const button = (node: Element, name: string) =>
      Array.from(node.querySelectorAll<HTMLButtonElement>('button')).find(b => b.textContent?.trim() === name)!;
    const settle = async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
      TestBed.tick();
    };
    return { ...opened, item, buttons, button, settle };
  }

  it('is a named dialog listing each schedule, and hands the list back to the rail', async () => {
    const { container, item } = await open();
    expectNamedDialog(container, { name: 'Schedules', description: /on this project’s assistant/ });
    expect(api.schedules).toHaveBeenCalledWith('prj_1');
    expect(changes).toEqual([[schedule(), THEIRS]]);

    const mine = item('Weekly digest').textContent!.replace(/\s+/g, ' ');
    expect(mine).toContain('Active');
    expect(mine).toContain('Shared with the project');
    expect(mine).toContain('Every Monday at 9:00 AM (America/Boise)');
    expect(mine).toContain('Made by you · Members are told about each run');
    expect(mine).toContain('Last run: never');

    const theirs = item('Roster check').textContent!.replace(/\s+/g, ' ');
    expect(theirs).toContain('Stopped');
    expect(theirs).toContain('Every 6 hours');
    expect(theirs).toContain('Made by Bo Diaz');
    expect(theirs).toContain('Stopped because its creator is no longer a member.');
    expect(theirs).not.toContain('Shared with the project');
  });

  it('says when a schedule publishes its artifacts to Outputs', async () => {
    api.schedules.mockReturnValue(of({ schedules: [schedule({ publishOutputs: true })] }));
    const { item } = await open();
    expect(item('Weekly digest').textContent!.replace(/\s+/g, ' ')).toContain(
      'Members are told about each run · Publishes its artifacts to Outputs',
    );
  });

  it('offers each action only where the schedule allows it', async () => {
    const { container, item, buttons } = await open();
    expect(buttons(container)).toContain('New schedule');
    expect(buttons(item('Weekly digest'))).toEqual(['Run history', 'Edit', 'Pause', 'Delete']);
    // An editor may stop someone else's, never resume or change it.
    expect(buttons(item('Roster check'))).toEqual(['Run history', 'Delete']);
  });

  it('is read-only for a viewer', async () => {
    api.schedules.mockReturnValue(of({ schedules: [schedule({ isMine: false, canEdit: false, canManage: false })] }));
    const { container, item, buttons } = await open({ ...PROJECT, role: 'viewer' });
    expect(buttons(container)).not.toContain('New schedule');
    expect(buttons(item('Weekly digest'))).toEqual(['Run history']);
    expect(grants.loadStatus).not.toHaveBeenCalled();
  });

  it('pauses, replacing the schedule with what came back', async () => {
    api.pauseSchedule.mockReturnValue(of(schedule({ state: 'paused', stateReason: 'Paused by user' })));
    const { item, button, buttons, settle } = await open();
    button(item('Weekly digest'), 'Pause').click();
    await settle();
    expect(api.pauseSchedule).toHaveBeenCalledWith('prj_1', 'sc_1');
    expect(item('Weekly digest').textContent).toContain('Paused');
    expect(buttons(item('Weekly digest'))).toEqual(['Run history', 'Edit', 'Resume', 'Delete']);
    expect(changes.at(-1)?.[0].state).toBe('paused');
  });

  it('shows a refused resume on the schedule it was about', async () => {
    api.schedules.mockReturnValue(of({ schedules: [schedule({ state: 'paused_error', stateReason: 'project_archived' })] }));
    api.resumeSchedule.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'This project is archived. Restore it to make changes.' } })),
    );
    const { item, button, settle } = await open();
    expect(item('Weekly digest').textContent).toContain('Stopped because the project was archived.');
    button(item('Weekly digest'), 'Resume').click();
    await settle();
    const alert = item('Weekly digest').querySelector('[role="alert"]');
    expect(alert?.textContent?.trim()).toBe('This project is archived. Restore it to make changes.');
  });

  it('deletes only after a confirmation, telling an editor the creator will hear about it', async () => {
    api.deleteSchedule.mockReturnValue(of(undefined));
    const { item, button, settle } = await open();
    const confirm = vi.spyOn(TestBed.inject(Dialog), 'open');
    confirm.mockReturnValueOnce({ closed: of(false) } as unknown as ReturnType<Dialog['open']>);
    button(item('Roster check'), 'Delete').click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(ConfirmationDialogComponent, {
      data: expect.objectContaining({ title: 'Delete “Roster check”?', destructive: true, message: expect.stringContaining('Bo Diaz is told') }),
    });
    expect(api.deleteSchedule).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce({ closed: of(true) } as unknown as ReturnType<Dialog['open']>);
    button(item('Roster check'), 'Delete').click();
    await settle();
    expect(api.deleteSchedule).toHaveBeenCalledWith('prj_1', 'sc_2');
    expect(item('Roster check')).toBeUndefined();
    expect(changes.at(-1)).toEqual([schedule()]);
  });

  it('expands a schedule’s run history, linking a delivered run to its share', async () => {
    api.scheduleRuns.mockReturnValue(
      of({
        runs: [
          { runId: 'r3', status: 'completed', finishedAt: '2026-10-08T16:00:00Z', shareId: 'sh_3' },
          { runId: 'r2', status: 'error', finishedAt: '2026-10-07T16:00:00Z', reason: 'model_error' },
          { runId: 'r1', status: 'paused', finishedAt: '2026-10-06T16:00:00Z', reason: 'not_editor' },
        ],
      }),
    );
    const { item, button, settle } = await open();
    const toggle = button(item('Weekly digest'), 'Run history');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    toggle.click();
    await settle();
    expect(api.scheduleRuns).toHaveBeenCalledWith('prj_1', 'sc_1');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const panel = item('Weekly digest').querySelector(`#${toggle.getAttribute('aria-controls')}`)!;
    const runs = Array.from(panel.querySelectorAll('li')).map(li => li.textContent!.replace(/\s+/g, ' ').trim());
    expect(runs[0]).toMatch(/^Completed and shared · Oct \d+, .*Open the shared run$/);
    expect(runs[1]).toMatch(/^Failed · Oct /);
    expect(runs[2]).toContain('Stopped because you’re no longer an editor.');
    expect(panel.querySelector('a')?.getAttribute('href')).toBe('/shared/sh_3');
  });

  it('says so when a schedule has not run', async () => {
    api.scheduleRuns.mockReturnValue(of({ runs: [] }));
    const { item, button, settle } = await open();
    button(item('Weekly digest'), 'Run history').click();
    await settle();
    expect(item('Weekly digest').textContent).toContain('It hasn’t run yet.');
  });

  it('opens the form in project mode for a new schedule or an edit', async () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const first = await open();
    const closed = vi.spyOn(first.ref, 'close');
    first.button(first.item('Weekly digest'), 'Edit').click();
    expect(navigate).toHaveBeenCalledWith(['/schedules', 'sc_1', 'edit'], { queryParams: { projectId: 'prj_1' } });
    expect(closed).toHaveBeenCalled();

    const second = await open();
    second.button(second.container, 'New schedule').click();
    expect(navigate).toHaveBeenLastCalledWith(['/schedules/new'], { queryParams: { projectId: 'prj_1' } });
  });

  it('asks an editor to turn on scheduled runs when their sign-in for them is missing', async () => {
    grants.status$.set({ enabled: false });
    grants.enable.mockImplementation(async () => grants.status$.set({ enabled: true }));
    const { container, button, settle } = await open();
    await settle();
    expect(container.textContent).toContain('Turn on scheduled runs so they can.');
    button(container, 'Turn on scheduled runs').click();
    await settle();
    expect(grants.enable).toHaveBeenCalled();
    expect(container.textContent).not.toContain('Turn on scheduled runs so they can.');
  });

  it('says scheduled runs are off when the routes 404, and tells the rail', async () => {
    api.schedules.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const { container, buttons } = await open();
    expect(container.textContent).toContain('Scheduled runs aren’t turned on here.');
    expect(buttons(container)).not.toContain('New schedule');
    expect(changes).toEqual([null]);
  });

  it('invites an editor to create the first schedule', async () => {
    api.schedules.mockReturnValue(of({ schedules: [] }));
    const { container } = await open();
    expect(container.textContent).toContain('No schedules yet.');
    expect(container.textContent).toContain('can share each run with the project');
  });
});
