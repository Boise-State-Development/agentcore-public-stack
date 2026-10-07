import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component, input } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectDetailPage } from './project-detail.page';
import { ProjectComposerComponent } from './project-composer.component';
import { ProjectProposalsComponent } from './project-proposals.component';
import { ProjectTasksComponent } from './project-tasks.component';
import { ProjectInstructionsDialogComponent } from '../components/project-instructions-dialog.component';
import { ProjectMembersDialogComponent } from '../components/project-members-dialog.component';
import { AgentService } from '../../agents/services/agent.service';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectApiService } from '../services/project-api.service';
import { Project } from '../models/project.model';

/**
 * The detail page through the router: `id` and `tab` are route params bound with
 * `withComponentInputBinding()`, which is exactly the timing a constructed component
 * would skip. The composer and the task list are stubbed (each has its own spec);
 * the rail is real, because the page's one piece of routing logic is handing a
 * legacy `tab` URL to it.
 */
@Component({ selector: 'app-project-composer', template: 'composer' })
class ComposerStub {
  readonly project = input<Project>();
  readonly modelLabel = input<string | null>();
}
@Component({ selector: 'app-project-tasks', template: 'tasks' })
class TasksStub {
  readonly project = input<Project>();
}
@Component({ selector: 'app-project-proposals', template: '' })
class ProposalsStub {
  readonly project = input<Project>();
}

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: 'The nightly sync.',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

describe('ProjectDetailPage', () => {
  const api = {
    get: vi.fn(),
    instructions: vi.fn(),
    model: vi.fn(),
    bindings: vi.fn(),
    files: vi.fn(),
  };
  const agents = { loadBindable: vi.fn() };
  const dialog = { open: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(PROJECT));
    api.instructions.mockReturnValue(of({ instructions: 'Be terse.', version: 2, canEdit: true }));
    api.model.mockReturnValue(of({ modelConfig: null, version: 2, canEdit: true }));
    api.bindings.mockReturnValue(of({ bindings: [], version: 2, canEdit: true }));
    api.files.mockReturnValue(of({ documents: [], nextToken: null, canEdit: true }));
    agents.loadBindable.mockResolvedValue([]);
    dialog.open.mockReturnValue({ closed: of(undefined), close: vi.fn() });
    TestBed.configureTestingModule({
      providers: [
        provideRouter(
          [
            { path: 'projects/:id/:tab', component: ProjectDetailPage },
            { path: 'projects/:id', redirectTo: 'projects/:id/overview' },
          ],
          withComponentInputBinding(),
        ),
        { provide: ProjectApiService, useValue: api },
        { provide: AgentService, useValue: agents },
        { provide: Dialog, useValue: dialog },
        { provide: ToastService, useValue: toast },
      ],
    });
    TestBed.overrideComponent(ProjectDetailPage, {
      remove: { imports: [ProjectComposerComponent, ProjectProposalsComponent, ProjectTasksComponent] },
      add: { imports: [ComposerStub, ProposalsStub, TasksStub] },
    });
  });

  async function open(url: string): Promise<{ harness: RouterTestingHarness; el: HTMLElement }> {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl(url);
    await new Promise(r => setTimeout(r, 0));
    harness.detectChanges();
    await new Promise(r => setTimeout(r, 0));
    harness.detectChanges();
    return { harness, el: harness.routeNativeElement as HTMLElement };
  }

  function railRows(el: HTMLElement): string[] {
    return Array.from(el.querySelectorAll('nav[aria-label="Project settings"] button')).map(
      b => b.querySelector('span > span')?.textContent?.trim() ?? '',
    );
  }

  it('loads the project from the route: its name in the title and the breadcrumb, the composer and the tasks', async () => {
    const { el } = await open('/projects/prj_1');
    expect(api.get).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h1')?.textContent).toContain('Enrollment Sync');
    expect(el.querySelector('nav[aria-label="Breadcrumb"] button')?.textContent).toContain('Enrollment Sync');
    expect(el.textContent).toContain('composer');
    expect(el.textContent).toContain('tasks');
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('lists the settings rail, with Activity for an editor', async () => {
    const { el } = await open('/projects/prj_1');
    expect(railRows(el)).toEqual(['Instructions', 'Files', 'Model', 'Tools & skills', 'Members', 'Activity', 'History']);
  });

  it('gives a viewer no Activity row', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'viewer' }));
    const { el } = await open('/projects/prj_1');
    expect(railRows(el)).not.toContain('Activity');
    expect(el.textContent).toContain('You can view it but not change it.');
  });

  it('opens the dialog a legacy tab URL names, then settles the URL on overview', async () => {
    const { harness } = await open('/projects/prj_1/members');
    expect(dialog.open).toHaveBeenCalledWith(ProjectMembersDialogComponent, expect.anything());
    expect(TestBed.inject(Router).url).toBe('/projects/prj_1/overview');
    harness.detectChanges();
    expect(dialog.open).toHaveBeenCalledTimes(1);
  });

  it('sends the old settings tab to the Instructions dialog', async () => {
    await open('/projects/prj_1/settings');
    expect(dialog.open).toHaveBeenCalledWith(
      ProjectInstructionsDialogComponent,
      expect.objectContaining({ data: expect.objectContaining({ instructions: 'Be terse.', version: 2, canEdit: true }) }),
    );
  });

  it('opens nothing for a viewer on /activity, and still settles the URL', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'viewer' }));
    await open('/projects/prj_1/activity');
    expect(dialog.open).not.toHaveBeenCalled();
    expect(TestBed.inject(Router).url).toBe('/projects/prj_1/overview');
  });

  it('opens nothing for a tab it does not know', async () => {
    await open('/projects/prj_1/nonsense');
    expect(dialog.open).not.toHaveBeenCalled();
    expect(TestBed.inject(Router).url).toBe('/projects/prj_1/overview');
  });

  it('tells a non-member the project is not there, without saying whether it exists', async () => {
    api.get.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const { el } = await open('/projects/prj_1/members');
    expect(el.querySelector('[role=alert]')?.textContent).toContain('doesn’t exist, or you’re not a member');
    expect(el.querySelector('nav[aria-label="Project settings"]')).toBeNull();
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('shows the archived notice, with the way back for the owner', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'owner', status: 'archived' }));
    const { el } = await open('/projects/prj_1/overview');
    const notice = el.querySelector('[role=status]')?.textContent ?? '';
    expect(notice).toContain('archived');
    expect(notice).toContain('restore it now');
  });
});
