import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component, input } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { of, throwError } from 'rxjs';
import { ProjectDetailPage } from './project-detail.page';
import { ProjectOverviewComponent } from './project-overview.component';
import { ProjectTasksComponent } from './project-tasks.component';
import { ProjectFilesComponent } from './project-files.component';
import { ProjectMembersComponent } from './project-members.component';
import { ProjectActivityComponent } from './project-activity.component';
import { ProjectSettingsComponent } from './project-settings.component';
import { ProjectApiService } from '../services/project-api.service';
import { Project } from '../models/project.model';

/**
 * The detail page through the router: `id` and `tab` are route params bound with
 * `withComponentInputBinding()`, which is exactly the timing a constructed component
 * would skip. The tabs are stubbed; each has its own behavior to test.
 */
@Component({ selector: 'app-project-overview', template: 'overview-tab' })
class OverviewStub { readonly project = input<Project>(); }
@Component({ selector: 'app-project-tasks', template: 'tasks-tab' })
class TasksStub { readonly project = input<Project>(); }
@Component({ selector: 'app-project-files', template: 'files-tab' })
class FilesStub { readonly project = input<Project>(); }
@Component({ selector: 'app-project-activity', template: 'activity-tab' })
class ActivityStub { readonly project = input<Project>(); }
@Component({ selector: 'app-project-members', template: 'members-tab' })
class MembersStub { readonly project = input<Project>(); }
@Component({ selector: 'app-project-settings', template: 'settings-tab' })
class SettingsStub { readonly project = input<Project>(); }

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: 'The nightly sync.',
  ownerEmail: 'o@x.edu',
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

describe('ProjectDetailPage', () => {
  const api = { get: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(PROJECT));
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
      ],
    });
    TestBed.overrideComponent(ProjectDetailPage, {
      remove: {
        imports: [ProjectOverviewComponent, ProjectTasksComponent, ProjectFilesComponent, ProjectMembersComponent, ProjectSettingsComponent, ProjectActivityComponent],
      },
      add: { imports: [OverviewStub, TasksStub, FilesStub, MembersStub, SettingsStub, ActivityStub] },
    });
  });

  async function open(url: string): Promise<{ harness: RouterTestingHarness; el: HTMLElement }> {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl(url);
    await new Promise(r => setTimeout(r, 0));
    harness.detectChanges();
    return { harness, el: harness.routeNativeElement as HTMLElement };
  }

  it('loads the project from the route and opens Overview by default', async () => {
    const { el } = await open('/projects/prj_1');
    expect(api.get).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h1')?.textContent).toContain('Enrollment Sync');
    expect(el.textContent).toContain('Editor');
    expect(el.textContent).toContain('overview-tab');
    expect(el.querySelector('[aria-current=page]')?.textContent?.trim()).toBe('Overview');
  });

  it('lists the tabs in order, with Activity for an editor', async () => {
    const { el } = await open('/projects/prj_1');
    const labels = Array.from(el.querySelectorAll('nav[aria-label="Project sections"] a')).map(a => a.textContent?.trim());
    expect(labels).toEqual(['Overview', 'Tasks', 'Files', 'Members', 'Settings', 'Activity']);
  });

  it('opens Activity for an editor', async () => {
    const { el } = await open('/projects/prj_1/activity');
    expect(el.textContent).toContain('activity-tab');
  });

  it('gives a viewer no Activity tab, and sends /activity to Overview', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'viewer' }));
    const { el } = await open('/projects/prj_1/activity');
    const labels = Array.from(el.querySelectorAll('nav[aria-label="Project sections"] a')).map(a => a.textContent?.trim());
    expect(labels).not.toContain('Activity');
    expect(el.textContent).not.toContain('activity-tab');
    expect(el.textContent).toContain('overview-tab');
  });

  it.each([
    ['tasks', 'tasks-tab', 'Tasks'],
    ['files', 'files-tab', 'Files'],
  ])('opens %s from the URL', async (tab, content, label) => {
    const { el } = await open(`/projects/prj_1/${tab}`);
    expect(el.textContent).toContain(content);
    expect(el.querySelector('[aria-current=page]')?.textContent?.trim()).toBe(label);
  });

  it('opens the tab named in the URL', async () => {
    const { el } = await open('/projects/prj_1/members');
    expect(el.textContent).toContain('members-tab');
    expect(el.querySelector('[aria-current=page]')?.textContent?.trim()).toBe('Members');
  });

  it('tells a non-member the project is not there, without saying whether it exists', async () => {
    api.get.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const { el } = await open('/projects/prj_1/settings');
    expect(el.querySelector('[role=alert]')?.textContent).toContain('doesn’t exist, or you’re not a member');
    expect(el.textContent).not.toContain('settings-tab');
  });

  it('shows the archived notice, with the way back for the owner', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'owner', status: 'archived' }));
    const { el } = await open('/projects/prj_1/overview');
    const notice = el.querySelector('[role=status]')?.textContent ?? '';
    expect(notice).toContain('archived');
    expect(notice).toContain('restore it from');
  });
});
