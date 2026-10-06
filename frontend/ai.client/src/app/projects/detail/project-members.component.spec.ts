import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { ProjectMembersComponent } from './project-members.component';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService } from '../services/projects.service';
import { UserService } from '../../auth/user.service';
import { ToastService } from '../../services/toast/toast.service';
import { Project, ProjectMember } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: 'Olive Owner',
  role: 'owner',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

const MEMBERS: ProjectMember[] = [
  { email: 'o@x.edu', name: 'Olive Owner', role: 'owner', hasSignedIn: true },
  // Signed in to the platform but has not opened the project: can still become owner.
  { email: 'ann@x.edu', name: 'Ann Lee', role: 'editor', hasSignedIn: true },
  { email: 'bo@x.edu', name: null, role: 'editor', hasSignedIn: false },
  { email: 'cy@x.edu', name: null, role: 'viewer', hasSignedIn: false },
];

describe('ProjectMembersComponent', () => {
  const api = { members: vi.fn(), directory: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.members.mockReturnValue(of({ members: MEMBERS, canManage: false }));
    TestBed.configureTestingModule({
      imports: [ProjectMembersComponent],
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: ProjectsService, useValue: { remove: vi.fn() } },
        { provide: UserService, useValue: { currentUser: signal({ email: 'O@x.edu' }) } },
        { provide: ToastService, useValue: { success: vi.fn(), warning: vi.fn() } },
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectMembersComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    // Each row's lines (name, email, status), one per <p>.
    const rows = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('li')).map(li =>
      Array.from(li.querySelectorAll('p')).map(p => p.textContent?.replace(/\s+/g, ' ').trim()).join(' | '),
    );
    return { el: fixture.nativeElement as HTMLElement, rows };
  }

  it('shows each person by name with their email beneath, or by email alone', async () => {
    const { rows } = await render();
    expect(rows).toEqual([
      'Olive Owner (you) | o@x.edu',
      'Ann Lee | ann@x.edu',
      'bo@x.edu | Can become owner once they’ve signed in',
      'cy@x.edu | Hasn’t signed in yet',
    ]);
  });

  it('offers "Make owner" for a signed-in editor and explains why it is withheld otherwise', async () => {
    const { el, rows } = await render();
    const makeOwner = Array.from(el.querySelectorAll('li')).map(li =>
      Array.from(li.querySelectorAll('button')).some(b => b.textContent?.includes('Make owner')),
    );
    expect(makeOwner).toEqual([false, true, false, false]);
    expect(rows[2]).toContain('Can become owner once they’ve signed in');
    expect(rows[3]).not.toContain('Can become owner');
  });

  it('says only "hasn’t signed in" to someone who is not the owner', async () => {
    const { rows } = await render({ ...PROJECT, role: 'editor' });
    expect(rows[2]).toContain('Hasn’t signed in yet');
    expect(rows[2]).not.toContain('Can become owner');
  });
});
