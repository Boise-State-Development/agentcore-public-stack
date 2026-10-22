import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ProjectProposalsComponent } from './project-proposals.component';
import { ProjectApiService } from '../services/project-api.service';
import { Project } from '../models/project.model';
import { MEMORY_PROJECT as PROJECT } from '../../../testing/project-memory.fixtures';

describe('ProjectProposalsComponent', () => {
  const api = { proposals: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.proposals.mockReturnValue(of({ proposals: [{ proposalId: 'p1' }, { proposalId: 'p2' }] }));
    TestBed.configureTestingModule({
      imports: [ProjectProposalsComponent],
      providers: [{ provide: ProjectApiService, useValue: api }, provideRouter([])],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectProposalsComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('says how many changes wait and links to the Memory tab’s review queue', async () => {
    const el = await render();
    expect(api.proposals).toHaveBeenCalledWith('prj_1', 'pending');
    expect(el.textContent).toContain('Proposed memory changes');
    expect(el.textContent).toContain('2 changes are waiting for review.');
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/projects/prj_1/memory?view=review');
  });

  it('renders nothing when nothing is pending', async () => {
    api.proposals.mockReturnValue(of({ proposals: [] }));
    expect((await render()).querySelector('section')).toBeNull();
  });

  it('stays quiet when the project has no shared memory (409)', async () => {
    api.proposals.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'no memory' } })));
    const el = await render();
    expect(el.querySelector('section')).toBeNull();
    expect(el.querySelector('[role=alert]')).toBeNull();
  });

  it('tells a member their own proposals are waiting', async () => {
    api.proposals.mockReturnValue(of({ proposals: [{ proposalId: 'p1' }] }));
    const el = await render({ ...PROJECT, role: 'viewer' });
    expect(el.textContent).toContain('Your proposed memory changes');
    expect(el.textContent).toContain('An editor will review them.');
  });
});
