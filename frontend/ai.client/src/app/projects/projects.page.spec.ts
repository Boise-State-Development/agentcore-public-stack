import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { ProjectsPage } from './projects.page';
import { ProjectsService } from './services/projects.service';
import { Project, ProjectRole, ProjectStatus } from './models/project.model';
import { environment } from '../../environments/environment';
import { FEATURES } from '../services/features';
import { ListArrangementService } from '../services/list-arrangement/list-arrangement.service';
import { ToastService } from '../services/toast/toast.service';
import { listArrangementStub } from '../../testing/list-arrangement';

const project = (id: string, role: ProjectRole, status: ProjectStatus = 'active'): Project => ({
  projectId: id,
  name: `Project ${id}`,
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role,
  status,
  editorsManageMembers: true,
  memberCount: 2,
  harnessAgentId: `ast-${id}`,
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
});

describe('ProjectsPage', () => {
  let arrangements: ReturnType<typeof listArrangementStub>;
  let organizeLists = false;
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
    arrangements = listArrangementStub();
    organizeLists = false;
    TestBed.configureTestingModule({
      imports: [ProjectsPage],
      providers: [
        provideRouter([]),
        { provide: ProjectsService, useValue: service },
        { provide: Dialog, useValue: { open: vi.fn() } },
        { provide: ListArrangementService, useFactory: () => arrangements },
        { provide: FEATURES, useFactory: () => ({ ...environment.features, organizeLists }) },
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

  // ── Hide (docs/specs/agent-project-organization.md) ────────────────────────────
  function renderOrganized() {
    organizeLists = true;
    const fixture = TestBed.createComponent(ProjectsPage);
    fixture.detectChanges();
    return fixture;
  }

  const chips = (el: HTMLElement) => [...el.querySelectorAll('[role=radio]')].map(b => b.textContent?.trim());
  const hideButton = (el: HTMLElement, name: string) =>
    el.querySelector<HTMLButtonElement>(`button[aria-label="Hide Project ${name}"]`);

  it('offers no Hide and no Hidden chip while the flag is off', () => {
    const fixture = TestBed.createComponent(ProjectsPage);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(hideButton(el, 'shared')).toBeNull();
    expect(chips(el)).not.toContain('Hidden (0)');
  });

  it('hides a project from All into Hidden, with Undo, and keeps it a sibling of the link', async () => {
    const fixture = renderOrganized();
    const el: HTMLElement = fixture.nativeElement;
    expect(arrangements.open).toHaveBeenCalledWith('projects');
    expect(chips(el)).toEqual(['All', 'Mine', 'Shared with me', 'Archived']);

    const button = hideButton(el, 'shared')!;
    expect(button.closest('a')).toBeNull();
    const info = vi.spyOn(TestBed.inject(ToastService), 'info');
    button.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(names(el)).toEqual(['Project mine', 'Project viewing']);
    expect(chips(el)).toContain('Hidden (1)');
    expect(info).toHaveBeenCalledWith('Hid Project shared', expect.any(String), expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) }));

    choose(el, 'Hidden (1)');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project shared']);

    info.mock.calls[0][2]!.action!.handler();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(names(el)).toEqual([]);
    expect(el.textContent).toContain('Nothing hidden.');
  });

  it('leaves a hidden project out of Mine and Shared, and labels it under Archived', () => {
    arrangements.hidden.projects.set(new Set(['viewing', 'old']));
    const fixture = renderOrganized();
    const el: HTMLElement = fixture.nativeElement;
    choose(el, 'Shared with me');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project shared']);
    expect(chips(el)).toContain('Hidden (1)');
    choose(el, 'Archived');
    fixture.detectChanges();
    expect(names(el)).toEqual(['Project old']);
    expect(el.querySelector('ul li')?.textContent).toContain('Hidden');
  });

  it('says so when a hide fails, and changes nothing', async () => {
    const fixture = renderOrganized();
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    arrangements.fail();
    hideButton(fixture.nativeElement, 'mine')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(error).toHaveBeenCalledWith('Could not hide project', 'Please try again.');
    expect(names(fixture.nativeElement)).toContain('Project mine');
  });
});
