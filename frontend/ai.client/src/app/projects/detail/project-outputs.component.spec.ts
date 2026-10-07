import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectOutputsComponent } from './project-outputs.component';
import { ProjectApiService } from '../services/project-api.service';
import { Project, ProjectOutput } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role: 'viewer',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

const THEIRS: ProjectOutput = {
  artifactId: 'art-1',
  shareId: 'sh-1',
  version: 2,
  title: 'Roster chart',
  contentType: 'text/html; charset=utf-8',
  sharedByEmail: 'ann@x.edu',
  sharedByName: 'Ann Lee',
  sharedAt: '2026-10-07T12:00:00Z',
  shareUrl: '/shared-artifact/sh-1',
  isMine: false,
  canRemove: false,
};
const MINE: ProjectOutput = {
  ...THEIRS, artifactId: 'art-2', shareId: 'sh-2', version: 1, title: 'Term dates', contentType: 'text/csv',
  sharedByEmail: 'me@x.edu', sharedByName: null, isMine: true, canRemove: true,
};

describe('ProjectOutputsComponent', () => {
  const api = { outputs: vi.fn(), removeOutput: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.outputs.mockReturnValue(of({ outputs: [THEIRS, MINE] }));
    api.removeOutput.mockReturnValue(of(undefined));
    dialog.open.mockReturnValue({ closed: of(true) });
    TestBed.configureTestingModule({
      imports: [ProjectOutputsComponent],
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectOutputsComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('lists outputs as cards that open the shared-artifact view', async () => {
    const { el } = await render();
    expect(api.outputs).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h2')?.textContent).toContain('Outputs');
    const links = Array.from(el.querySelectorAll('a'));
    expect(links.map(a => a.getAttribute('href'))).toEqual(['/shared-artifact/sh-1', '/shared-artifact/sh-2']);
    expect(links.map(a => a.getAttribute('aria-label'))).toEqual([
      'Open Web page Roster chart, shared by Ann Lee',
      'Open CSV Term dates, shared by you',
    ]);
    const text = el.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('Roster chart');
    expect(text).toContain('Web page · v2 · Shared by Ann Lee');
    expect(text).toContain('CSV · Shared by you');
  });

  it('offers removal only where the caller may remove', async () => {
    const { el } = await render();
    const buttons = Array.from(el.querySelectorAll('button')).map(b => b.getAttribute('aria-label'));
    expect(buttons).toEqual(['Stop sharing Term dates']);
  });

  it('removes after confirmation', async () => {
    const { el, fixture } = await render();
    (el.querySelector('button[aria-label="Stop sharing Term dates"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.removeOutput).toHaveBeenCalledWith('prj_1', 'art-2');
    expect(Array.from(el.querySelectorAll('a')).length).toBe(1);
  });

  it('does nothing when the confirmation is cancelled', async () => {
    dialog.open.mockReturnValue({ closed: of(undefined) });
    const { el, fixture } = await render();
    (el.querySelector('button[aria-label="Stop sharing Term dates"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    expect(api.removeOutput).not.toHaveBeenCalled();
  });

  it('renders nothing until something is shared', async () => {
    api.outputs.mockReturnValue(of({ outputs: [] }));
    const { el } = await render();
    expect(el.querySelector('section')).toBeNull();
  });

  it('shows the API sentence when the list fails', async () => {
    api.outputs.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500, error: { detail: 'Projects are having trouble.' } })));
    const { el } = await render();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Projects are having trouble.');
  });
});
