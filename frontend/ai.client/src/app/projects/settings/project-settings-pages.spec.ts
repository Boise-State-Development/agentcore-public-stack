import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Component, input, output } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService } from '../services/projects.service';
import { TEST_PROJECT, openProjectPage, provideProjectPageRoute } from '../../../testing/project-page';
import { ProjectActivityComponent } from './project-activity.component';
import { ProjectActivityPage } from './project-activity.page';
import { ProjectFilesComponent } from './project-files.component';
import { ProjectFilesPage } from './project-files.page';
import { ProjectHistoryComponent } from './project-history.component';
import { ProjectHistoryPage } from './project-history.page';
import { ProjectMembersComponent } from './project-members.component';
import { ProjectMembersPage } from './project-members.page';

@Component({ selector: 'app-project-files', template: 'files list' })
class FilesStub {
  readonly project = input<Project>();
}
@Component({ selector: 'app-project-members', template: 'members list' })
class MembersStub {
  readonly project = input<Project>();
  readonly projectChange = output<Project>();
}
@Component({ selector: 'app-project-activity', template: 'activity list' })
class ActivityStub {
  readonly project = input<Project>();
}
@Component({ selector: 'app-project-history', template: 'history of {{ projectId() }} at [{{ initialVersion() }}]' })
class HistoryStub {
  readonly projectId = input<string>();
  readonly initialVersion = input<number | null>(null);
}

/**
 * The pages that only frame a list (Files, Members, Activity, History): each loads
 * its project from the route, titles itself, and hands the project to the list,
 * which has its own spec.
 */
describe('project settings pages', () => {
  const api = { get: vi.fn() };
  const projects = { upsert: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(TEST_PROJECT));
    TestBed.overrideComponent(ProjectFilesPage, { remove: { imports: [ProjectFilesComponent] }, add: { imports: [FilesStub] } });
    TestBed.overrideComponent(ProjectMembersPage, { remove: { imports: [ProjectMembersComponent] }, add: { imports: [MembersStub] } });
    TestBed.overrideComponent(ProjectActivityPage, { remove: { imports: [ProjectActivityComponent] }, add: { imports: [ActivityStub] } });
    TestBed.overrideComponent(ProjectHistoryPage, { remove: { imports: [ProjectHistoryComponent] }, add: { imports: [HistoryStub] } });
  });

  function route(path: string, component: unknown) {
    TestBed.configureTestingModule({
      providers: [
        provideProjectPageRoute(path, component as never),
        { provide: ProjectApiService, useValue: api },
        { provide: ProjectsService, useValue: projects },
      ],
    });
  }

  it('Files: titled and described, the list beneath', async () => {
    route('files', ProjectFilesPage);
    const { el } = await openProjectPage('/projects/prj_1/files');
    expect(api.get).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h1')?.textContent).toContain('Files');
    expect(el.querySelector('[aria-current="page"]')?.textContent).toContain('Files');
    expect(el.textContent).toContain('Documents the project’s assistant works from.');
    expect(el.textContent).toContain('files list');
  });

  it('Members: keeps a change the list reports, and hands it to the sidebar’s list', async () => {
    route('members', ProjectMembersPage);
    const { el, harness } = await openProjectPage('/projects/prj_1/members');
    expect(el.textContent).toContain('4 people work with the same assistant in this project.');
    const stub = harness.routeDebugElement!.query(d => d.componentInstance instanceof MembersStub).componentInstance as MembersStub;
    stub.projectChange.emit({ ...TEST_PROJECT, memberCount: 4 });
    harness.detectChanges();
    expect(el.textContent).toContain('5 people work with the same assistant');
    expect(projects.upsert).toHaveBeenCalledWith({ ...TEST_PROJECT, memberCount: 4 });
  });

  it('Activity: the trail for an editor', async () => {
    route('activity', ProjectActivityPage);
    expect((await openProjectPage('/projects/prj_1/activity')).el.textContent).toContain('activity list');
  });

  it('Activity: a plain explanation for a viewer, without asking for the trail', async () => {
    api.get.mockReturnValue(of({ ...TEST_PROJECT, role: 'viewer' }));
    route('activity', ProjectActivityPage);
    const viewer = (await openProjectPage('/projects/prj_1/activity')).el.textContent;
    expect(viewer).not.toContain('activity list');
    expect(viewer).toContain('Only editors and the owner can see this project’s activity.');
  });

  it('History: opens the version a link names, and ignores one that is not a version', async () => {
    route('history', ProjectHistoryPage);
    const first = await openProjectPage('/projects/prj_1/history?version=3');
    expect(first.el.textContent).toContain('history of prj_1 at [3]');
    expect((await openProjectPage('/projects/prj_1/history?version=x', first.harness)).el.textContent).toContain('history of prj_1 at []');
    expect((await openProjectPage('/projects/prj_1/history', first.harness)).el.textContent).toContain('history of prj_1 at []');
  });
});
