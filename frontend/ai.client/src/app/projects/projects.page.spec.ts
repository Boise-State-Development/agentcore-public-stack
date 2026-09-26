import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { ProjectsPage } from './projects.page';
import { ProjectsService } from './services/projects.service';
import { Project, ProjectRole, ProjectStatus } from './models/project.model';

const project = (id: string, role: ProjectRole, status: ProjectStatus = 'active'): Project => ({
  projectId: id,
  name: `Project ${id}`,
  description: '',
  ownerEmail: 'o@x.edu',
  role,
  status,
  editorsManageMembers: true,
  memberCount: 2,
  harnessAgentId: `ast-${id}`,
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
});

describe('ProjectsPage', () => {
  const projects = signal<Project[]>([]);
  const available = signal<boolean | null>(true);
  const service = {
    projects$: projects,
    loading$: signal(false),
    error$: signal<string | null>(null),
    available$: available,
    load: vi.fn().mockResolvedValue(undefined),
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    projects.set([
      project('mine', 'owner'),
      project('shared', 'editor'),
      project('viewing', 'viewer'),
      project('old', 'owner', 'archived'),
    ]);
    available.set(true);
    TestBed.configureTestingModule({
      imports: [ProjectsPage],
      providers: [
        provideRouter([]),
        { provide: ProjectsService, useValue: service },
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  function names(el: HTMLElement): string[] {
    return [...el.querySelectorAll('ul li a')].map(a => a.querySelector('span.text-base\\/7')?.textContent?.trim() ?? '');
  }

  function choose(el: HTMLElement, label: string): void {
    ([...el.querySelectorAll('[role=radio]')].find(b => b.textContent?.trim() === label) as HTMLButtonElement).click();
  }

  it('shows active projects under All, and splits Mine, Shared with me and Archived', () => {
    const fixture = TestBed.createComponent(ProjectsPage);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;

    expect(names(el)).toEqual(['Project mine', 'Project shared', 'Project viewing']);
    choose(el, 'Mine');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project mine']);
    choose(el, 'Shared with me');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project shared', 'Project viewing']);
    choose(el, 'Archived');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project old']);
    expect(service.load).toHaveBeenCalled();
  });

  it('says Projects are switched off instead of offering to create one', () => {
    available.set(false);
    const fixture = TestBed.createComponent(ProjectsPage);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Projects aren’t available here');
    expect([...el.querySelectorAll('button')].some(b => b.textContent?.includes('New project'))).toBe(false);
  });
});
