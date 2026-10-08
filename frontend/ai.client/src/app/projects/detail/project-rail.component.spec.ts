import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ProjectRailComponent } from './project-rail.component';
import { ProjectBindingsDialogComponent } from '../components/project-bindings-dialog.component';
import { ProjectFilesDialogComponent, ProjectFilesDialogData } from '../components/project-files-dialog.component';
import { ProjectInstructionsDialogComponent } from '../components/project-instructions-dialog.component';
import { ProjectMembersDialogComponent, ProjectMembersDialogData } from '../components/project-members-dialog.component';
import { ProjectModelDialogComponent } from '../components/project-model-dialog.component';
import { AgentService } from '../../agents/services/agent.service';
import { ProjectApiService } from '../services/project-api.service';
import { Project } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: 'Olive Owner',
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

const PALETTE = {
  model: [{ kind: 'model', ref: 'm-haiku', label: 'Haiku 4.5', description: '', meta: { provider: 'bedrock' } }],
  tool: [
    { kind: 'tool', ref: 't1', label: 'Canvas', description: '', meta: {} },
    { kind: 'tool', ref: 't2', label: 'Web', description: '', meta: {} },
  ],
  skill: [{ kind: 'skill', ref: 's1', label: 'Memo', description: '', meta: {} }],
};

describe('ProjectRailComponent', () => {
  const api = { instructions: vi.fn(), model: vi.fn(), bindings: vi.fn(), files: vi.fn(), memory: vi.fn(), memoryEntries: vi.fn() };
  const agents = { loadBindable: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.instructions.mockReturnValue(of({ instructions: '', version: 2, canEdit: true }));
    api.model.mockReturnValue(of({ modelConfig: { modelId: 'm-haiku' }, version: 2, canEdit: true }));
    api.bindings.mockImplementation((_id: string, kind: 'tools' | 'skills') =>
      of({ bindings: kind === 'tools' ? [{ ref: 't1' }, { ref: 'gone' }] : [{ ref: 's1' }], version: 2, canEdit: true }),
    );
    api.files.mockReturnValue(of({ documents: [{ documentId: 'a' }, { documentId: 'b' }, { documentId: 'c' }], nextToken: null, canEdit: true }));
    api.memory.mockReturnValue(of({ sharedSpaceId: 'spc_1', personalSpaceId: null, role: 'editor' }));
    api.memoryEntries.mockReturnValue(of({ entries: [{ slug: 'a' }, { slug: 'b' }] }));
    agents.loadBindable.mockImplementation((kind: keyof typeof PALETTE) => Promise.resolve(PALETTE[kind]));
    dialog.open.mockReturnValue({ closed: of(undefined), close: vi.fn() });
    TestBed.configureTestingModule({
      imports: [ProjectRailComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: AgentService, useValue: agents },
        { provide: Dialog, useValue: dialog },
        provideRouter([]),
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectRailComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    await new Promise(r => setTimeout(r, 0));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const row = (label: string) =>
      Array.from(el.querySelectorAll<HTMLButtonElement>('nav button')).find(b => b.textContent?.includes(label))!;
    return { fixture, el, row, component: fixture.componentInstance };
  }

  /** A row's label, meta and verb, space-separated (they are sibling spans with no whitespace between). */
  function text(node: Element | undefined): string {
    return Array.from(node?.querySelectorAll('span') ?? [])
      .filter(span => span.children.length === 0)
      .map(span => span.textContent?.trim() ?? '')
      .filter(Boolean)
      .join(' ');
  }

  it('summarises each setting on one line, with a verb for what the row lets you do', async () => {
    const { row, el } = await render();
    expect(text(row('Instructions'))).toBe('Instructions Not set Edit');
    expect(text(row('Files'))).toBe('Files 3 files Add');
    expect(text(row('Model'))).toBe('Model Haiku 4.5 Change');
    expect(text(row('Tools'))).toBe('Tools 2 tools Edit');
    expect(text(row('Skills'))).toBe('Skills 1 skill Edit');
    expect(text(row('Members'))).toBe('Members 4 people Manage');
    expect(text(row('History'))).toBe('History Version 2 View');
    expect(el.textContent).toContain('Shared with 3 other people. Owned by Olive Owner. You’re an editor.');
  });

  it('links Memory to its own page, with the shared memory’s file count', async () => {
    const { el } = await render();
    const link = Array.from(el.querySelectorAll<HTMLAnchorElement>('nav a')).find(a => a.textContent?.includes('Memory'))!;
    expect(text(link)).toBe('Memory 2 files Open');
    expect(link.getAttribute('href')).toBe('/projects/prj_1/memory');
    expect(api.memoryEntries).toHaveBeenCalledWith('spc_1');
  });

  it('leaves the Memory count blank when memory is unavailable', async () => {
    api.memory.mockReturnValue(throwError(() => new Error('off')));
    const { el } = await render();
    const link = Array.from(el.querySelectorAll<HTMLAnchorElement>('nav a')).find(a => a.textContent?.includes('Memory'))!;
    expect(text(link)).toBe('Memory Open');
  });

  it('reads for a viewer, and keeps Activity for editors only', async () => {
    const { row, el } = await render({ ...PROJECT, role: 'viewer' });
    expect(text(row('Instructions'))).toBe('Instructions Not set View');
    expect(text(row('Members'))).toBe('Members 4 people View');
    expect(el.textContent).not.toContain('Activity');
    expect(await render().then(r => r.el.textContent)).toContain('Activity');
  });

  it('names the model id when the catalog lacks it, and says when none is set', async () => {
    api.model.mockReturnValue(of({ modelConfig: { modelId: 'm-secret' }, version: 2, canEdit: true }));
    expect(text((await render()).row('Model'))).toBe('Model m-secret Change');
    api.model.mockReturnValue(of({ modelConfig: null, version: 2, canEdit: true }));
    expect(text((await render()).row('Model'))).toBe('Model Each member’s default Change');
  });

  it('keeps the rail up when one read fails', async () => {
    api.files.mockReturnValue(throwError(() => new Error('nope')));
    agents.loadBindable.mockRejectedValue(new Error('nope'));
    const { row } = await render();
    expect(text(row('Instructions'))).toBe('Instructions Not set Edit');
    expect(text(row('Model'))).toBe('Model m-haiku Change');
    expect(text(row('Files'))).toBe('Files Add');
  });

  it('opens the Instructions dialog with what it has, and shows what came back', async () => {
    dialog.open.mockReturnValue({ closed: of({ instructions: 'Be terse.', version: 3, canEdit: true }), close: vi.fn() });
    const { row, fixture } = await render();
    row('Instructions').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(dialog.open).toHaveBeenCalledWith(ProjectInstructionsDialogComponent, {
      data: { projectId: 'prj_1', canEdit: true, projectName: 'Enrollment Sync', instructions: '', version: 2 },
    });
    expect(text(row('Instructions'))).toBe('Instructions Edit');
    expect(text(row('History'))).toBe('History Version 3 View');
  });

  it('opens the Model and Tools dialogs with the palettes, and applies their saves', async () => {
    dialog.open.mockReturnValueOnce({ closed: of({ modelConfig: { modelId: 'm-secret' }, version: 3, canEdit: true }), close: vi.fn() });
    const { row, fixture } = await render();
    row('Model').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(dialog.open).toHaveBeenLastCalledWith(ProjectModelDialogComponent, {
      data: { projectId: 'prj_1', canEdit: true, modelId: 'm-haiku', models: PALETTE.model },
    });
    expect(text(row('Model'))).toBe('Model m-secret Change');

    dialog.open.mockReturnValueOnce({
      closed: of({ bindings: [{ ref: 't1' }, { ref: 't2' }, { ref: 'gone' }], version: 4, canEdit: true }),
      close: vi.fn(),
    });
    row('Tools').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(dialog.open).toHaveBeenLastCalledWith(ProjectBindingsDialogComponent, {
      data: { projectId: 'prj_1', canEdit: true, kind: 'tools', bound: ['t1', 'gone'], palette: PALETTE.tool },
    });
    expect(text(row('Tools'))).toBe('Tools 3 tools Edit');
    expect(text(row('Skills'))).toBe('Skills 1 skill Edit');
    expect(text(row('History'))).toBe('History Version 4 View');
  });

  it('opens the Skills dialog with each skill’s pin, and takes back a save or an Update', async () => {
    api.bindings.mockImplementation((_id: string, kind: 'tools' | 'skills') =>
      of({
        bindings: kind === 'tools' ? [] : [{ ref: 's1', version: 2, pinnedAt: '2026-10-01T00:00:00Z', updateAvailable: true }],
        version: 2,
        canEdit: true,
      }),
    );
    dialog.open.mockReturnValueOnce({
      closed: of({ bindings: [{ ref: 's1', version: 3, pinnedAt: '2026-10-08T00:00:00Z', updateAvailable: false }], version: 5, canEdit: true }),
      close: vi.fn(),
    });
    const { row, fixture } = await render();
    expect(text(row('Tools'))).toBe('Tools None Edit');
    expect(text(row('Skills'))).toBe('Skills 1 skill · 1 update Edit');

    row('Skills').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(dialog.open).toHaveBeenLastCalledWith(ProjectBindingsDialogComponent, {
      data: {
        projectId: 'prj_1',
        canEdit: true,
        kind: 'skills',
        bound: ['s1'],
        palette: PALETTE.skill,
        pins: { s1: { ref: 's1', version: 2, pinnedAt: '2026-10-01T00:00:00Z', updateAvailable: true } },
      },
    });
    expect(text(row('Skills'))).toBe('Skills 1 skill Edit');
    expect(text(row('History'))).toBe('History Version 5 View');
  });

  it('hands the Files dialog a live project and takes its count back', async () => {
    const { row, fixture } = await render();
    row('Files').click();
    await fixture.whenStable();
    const data = dialog.open.mock.calls[0][1].data as ProjectFilesDialogData;
    expect(dialog.open).toHaveBeenCalledWith(ProjectFilesDialogComponent, expect.anything());
    expect(data.project()).toEqual(PROJECT);
    data.onCountChange!(0);
    fixture.detectChanges();
    expect(text(row('Files'))).toBe('Files None yet Add');
  });

  it('relays a project change out of the Members dialog', async () => {
    const { row, component, fixture } = await render();
    const changed: Project[] = [];
    component.projectChange.subscribe(p => changed.push(p));
    row('Members').click();
    await fixture.whenStable();
    const data = dialog.open.mock.calls[0][1].data as ProjectMembersDialogData;
    expect(dialog.open).toHaveBeenCalledWith(ProjectMembersDialogComponent, expect.anything());
    data.onProjectChange({ ...PROJECT, memberCount: 4 });
    expect(changed).toEqual([{ ...PROJECT, memberCount: 4 }]);
  });

  it('closes the open dialog before opening another', async () => {
    const first = { closed: of(undefined), close: vi.fn() };
    dialog.open.mockReturnValueOnce(first);
    const { component } = await render();
    void component.open('activity');
    await component.open('history');
    expect(first.close).toHaveBeenCalled();
    expect(dialog.open).toHaveBeenCalledTimes(2);
  });

  it('refuses Activity for a viewer', async () => {
    const { component } = await render({ ...PROJECT, role: 'viewer' });
    await component.open('activity');
    expect(dialog.open).not.toHaveBeenCalled();
  });
});
