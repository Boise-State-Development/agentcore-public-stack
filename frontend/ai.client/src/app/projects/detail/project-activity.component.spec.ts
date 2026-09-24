import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ProjectActivityComponent, describeActivity } from './project-activity.component';
import { ProjectApiService } from '../services/project-api.service';
import { UserService } from '../../auth/user.service';
import { Project, ProjectAuditRecord } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1', name: 'Enrollment Sync', description: '', ownerEmail: 'o@x.edu', role: 'editor',
  status: 'active', editorsManageMembers: true, memberCount: 2, harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z', updatedAt: '2026-09-24T00:00:00Z',
};

function rec(action: string, over: Partial<ProjectAuditRecord> = {}): ProjectAuditRecord {
  return { auditId: `a-${action}-${Math.random()}`, timestamp: '2026-09-24T12:00:00Z', action, actorEmail: 'ann@x.edu', ...over };
}

describe('describeActivity', () => {
  it.each([
    [rec('project.created'), 'created the project'],
    [rec('project.updated', { changes: ['name'], after: { name: 'Roster' } }), 'renamed the project to “Roster”'],
    [rec('project.updated', { changes: ['description', 'editorsManageMembers'], after: { editorsManageMembers: false } }),
      'updated the description and limited managing members to the owner'],
    [rec('project.archived'), 'archived the project'],
    [rec('project.archived', { reason: 'policy review' }), 'archived the project (policy review)'],
    [rec('project.restored'), 'restored the project'],
    [rec('project.transferred', { after: { ownerEmail: 'bo@x.edu' } }), 'made bo@x.edu the owner'],
    [rec('project.member_added', { after: { email: 'bo@x.edu', role: 'viewer' } }), 'added bo@x.edu as a viewer'],
    [rec('project.member_role_changed', { before: { email: 'bo@x.edu', role: 'viewer' }, after: { email: 'bo@x.edu', role: 'editor' } }),
      'made bo@x.edu an editor (was a viewer)'],
    [rec('project.member_removed', { before: { email: 'bo@x.edu', role: 'viewer' } }), 'removed bo@x.edu'],
    [rec('project.member_removed', { before: { email: 'ann@x.edu' }, reason: 'left' }), 'left the project'],
    [rec('project.model_updated', { after: { version: 4, modelId: 'claude-x' } }), 'changed the model to claude-x'],
    [rec('project.tools_updated', { before: { refs: ['a', 'b'] }, after: { version: 5, refs: ['b', 'c', 'd'] } }),
      'added the tools c and d and removed the tool a'],
    [rec('project.skills_updated', { before: { refs: [] }, after: { version: 6, refs: [] } }), 'updated the skills'],
    [rec('project.knowledge_added', { after: { filename: 'plan.pdf', source: 'upload' } }), 'added the file plan.pdf'],
    [rec('project.knowledge_added', { after: { url: 'https://x.edu', source: 'web' } }), 'started adding pages from https://x.edu'],
    [rec('project.knowledge_removed', { before: { filename: 'plan.pdf' } }), 'removed the file plan.pdf'],
    [rec('project.task_shared', { after: { title: 'Roster diff' } }), 'shared the task “Roster diff” with the project'],
    [rec('project.task_unshared', { after: { title: 'Roster diff' } }), 'stopped sharing the task “Roster diff”'],
    [rec('project.something_new'), 'something new'],
  ])('%#: %s', (record, text) => {
    expect(describeActivity(record).text).toBe(text);
  });

  it('points a settings save at the version it cut', () => {
    expect(describeActivity(rec('project.instructions_updated', { after: { version: 3 } }))).toEqual({
      text: 'updated the instructions', version: 3,
    });
    // As the audit log returns it: detail values come back as strings.
    expect(describeActivity(rec('project.tools_updated', { after: { version: '7', refs: [] } })).version).toBe(7);
    expect(describeActivity(rec('project.member_added', { after: { email: 'x', role: 'viewer' } })).version).toBeNull();
  });
});

describe('ProjectActivityComponent', () => {
  const api = { audit: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    TestBed.configureTestingModule({
      imports: [ProjectActivityComponent],
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: UserService, useValue: { currentUser: signal({ email: 'Me@x.edu' }) } },
      ],
    });
  });

  async function render() {
    const fixture = TestBed.createComponent(ProjectActivityComponent);
    fixture.componentRef.setInput('project', PROJECT);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('lists the trail with who did it, "You" for the caller, and a link to the version', async () => {
    api.audit.mockReturnValue(of({
      records: [
        rec('project.instructions_updated', { actorEmail: 'me@x.edu', after: { version: 2 } }),
        rec('project.member_added', { after: { email: 'bo@x.edu', role: 'editor' } }),
      ],
      nextCursor: null,
    }));
    const { el } = await render();
    expect(api.audit).toHaveBeenCalledWith('prj_1', 50, null);
    const rows = Array.from(el.querySelectorAll('li')).map(li => li.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows[0]).toContain('You updated the instructions. Version 2');
    expect(rows[1]).toContain('ann@x.edu added bo@x.edu as an editor.');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/projects/prj_1/settings');
  });

  it('pages older entries with the cursor', async () => {
    api.audit.mockReturnValueOnce(of({ records: [rec('project.created')], nextCursor: 'c1' }));
    const { fixture, el } = await render();
    api.audit.mockReturnValueOnce(of({ records: [rec('project.archived')], nextCursor: null }));
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Show older'))!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.audit).toHaveBeenLastCalledWith('prj_1', 50, 'c1');
    expect(el.querySelectorAll('li').length).toBe(2);
    expect(el.textContent).not.toContain('Show older');
  });

  it('shows the API’s sentence on failure', async () => {
    api.audit.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'Only editors can see this.' } })));
    const { el } = await render();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Only editors can see this.');
  });

  it('says when nothing has happened', async () => {
    api.audit.mockReturnValue(of({ records: [], nextCursor: null }));
    const { el } = await render();
    expect(el.textContent).toContain('Nothing has happened here yet.');
  });
});
