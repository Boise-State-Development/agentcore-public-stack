import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Router, provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectTasksComponent } from './project-tasks.component';
import { ProjectApiService } from '../services/project-api.service';
import { ShareService } from '../../session/services/share/share.service';
import { SessionService } from '../../session/services/session/session.service';
import { Project, SharedTask } from '../models/project.model';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  role: 'viewer',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 2,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

function session(id: string, title: string): SessionMetadata {
  return {
    sessionId: id, userId: 'u', title, status: 'active', messageCount: 2,
    createdAt: '2026-09-20T00:00:00Z', lastMessageAt: '2026-09-23T12:00:00Z',
    preferences: { assistantId: 'ast-1', projectId: 'prj_1' },
  };
}

const MINE: SharedTask = { shareId: 'sh_1', title: 'Roster diff', sharedByEmail: 'me@x.edu', sharedAt: '2026-09-23T00:00:00Z', shareUrl: '/shared/sh_1', isMine: true };
const THEIRS: SharedTask = { shareId: 'sh_2', title: 'Term dates', sharedByEmail: 'ann@x.edu', sharedAt: '2026-09-22T00:00:00Z', shareUrl: '/shared/sh_2', isMine: false };

describe('ProjectTasksComponent', () => {
  const api = { tasks: vi.fn(), sharedTasks: vi.fn() };
  const shares = { exportSharedConversation: vi.fn(), revokeShare: vi.fn() };
  const sessions = { refreshSessions: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.tasks.mockReturnValue(of({ sessions: [session('s1', 'Draft the memo')], nextToken: 'cur_1' }));
    api.sharedTasks.mockReturnValue(of({ tasks: [MINE, THEIRS] }));
    TestBed.configureTestingModule({
      imports: [ProjectTasksComponent],
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: ShareService, useValue: shares },
        { provide: SessionService, useValue: sessions },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectTasksComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('lists the caller’s tasks, each opening on the project’s agent', async () => {
    const { el } = await render();
    expect(api.tasks).toHaveBeenCalledWith('prj_1', 20, null);
    const link = el.querySelector<HTMLAnchorElement>('a[href^="/s/"]');
    expect(link?.textContent).toContain('Draft the memo');
    expect(link?.getAttribute('href')).toBe('/s/s1?assistantId=ast-1');
  });

  it('pages with the value cursor and appends', async () => {
    const { fixture, el } = await render();
    api.tasks.mockReturnValue(of({ sessions: [session('s0', 'Older one')], nextToken: null }));
    const more = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Show more'))!;
    more.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.tasks).toHaveBeenLastCalledWith('prj_1', 20, 'cur_1');
    expect(el.textContent).toContain('Draft the memo');
    expect(el.textContent).toContain('Older one');
    expect(Array.from(el.querySelectorAll('button')).some(b => b.textContent?.includes('Show more'))).toBe(false);
  });

  it('lists shared tasks with who shared them, opening the shared view; only the sharer can revoke', async () => {
    const { el } = await render();
    expect(el.querySelector('a[href="/shared/sh_1"]')?.textContent).toContain('Roster diff');
    expect(el.textContent).toContain('Shared by you');
    expect(el.textContent).toContain('Shared by ann@x.edu');
    const revokes = el.querySelectorAll('button[aria-label^="Stop sharing"]');
    expect(revokes.length).toBe(1);
    expect(revokes[0].getAttribute('aria-label')).toBe('Stop sharing Roster diff');
  });

  it('continues a shared task as a fork on the project’s agent', async () => {
    shares.exportSharedConversation.mockResolvedValue({ sessionId: 'fork_1', title: 'Term dates (shared)' });
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    const { fixture, el } = await render();
    const buttons = Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes('Continue in my own task'));
    buttons[1].click();
    await fixture.whenStable();
    expect(shares.exportSharedConversation).toHaveBeenCalledWith('sh_2', { suppressErrorToast: true });
    expect(sessions.refreshSessions).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith(['/s', 'fork_1'], { queryParams: { assistantId: 'ast-1' } });
  });

  it('forks from an archived project without the agent (the backend returns a plain session)', async () => {
    shares.exportSharedConversation.mockResolvedValue({ sessionId: 'fork_2', title: 'x' });
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { fixture, el } = await render({ ...PROJECT, status: 'archived' });
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Continue'))!.click();
    await fixture.whenStable();
    expect(navigate).toHaveBeenCalledWith(['/s', 'fork_2'], { queryParams: {} });
  });

  it('revokes after confirmation and re-reads the list (an older share may take its place)', async () => {
    dialog.open.mockReturnValue({ closed: of(true) });
    shares.revokeShare.mockResolvedValue(undefined);
    const { fixture, el } = await render();
    api.sharedTasks.mockReturnValue(of({ tasks: [THEIRS] }));
    el.querySelector<HTMLButtonElement>('button[aria-label="Stop sharing Roster diff"]')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(shares.revokeShare).toHaveBeenCalledWith('sh_1', { suppressErrorToast: true });
    expect(el.textContent).not.toContain('Roster diff');
  });

  it('does not revoke when the confirmation is cancelled', async () => {
    dialog.open.mockReturnValue({ closed: of(false) });
    const { fixture, el } = await render();
    el.querySelector<HTMLButtonElement>('button[aria-label="Stop sharing Roster diff"]')!.click();
    await fixture.whenStable();
    expect(shares.revokeShare).not.toHaveBeenCalled();
  });

  it('shows the API’s sentence when a list fails', async () => {
    api.sharedTasks.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'You are not a member of this project.' } })),
    );
    const { el } = await render();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('You are not a member of this project.');
  });

  it('says when there is nothing yet', async () => {
    api.tasks.mockReturnValue(of({ sessions: [], nextToken: null }));
    api.sharedTasks.mockReturnValue(of({ tasks: [] }));
    const { el } = await render();
    expect(el.textContent).toContain('You haven’t started a task in this project yet.');
    expect(el.textContent).toContain('No one has shared a task with the project yet.');
  });
});
