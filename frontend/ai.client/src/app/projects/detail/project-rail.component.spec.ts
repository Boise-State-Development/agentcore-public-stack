import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ProjectRailComponent } from './project-rail.component';
import { AgentService } from '../../agents/services/agent.service';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectApiService } from '../services/project-api.service';
import { Project, ProjectSchedule } from '../models/project.model';

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

const MODELS = [
  { kind: 'model', ref: 'm-haiku', label: 'Haiku 4.5', description: 'Fast', meta: { provider: 'bedrock' } },
  { kind: 'model', ref: 'm-sonnet', label: 'Sonnet 4.6', description: '', meta: { provider: 'bedrock' } },
];

function sched(scheduleId: string, state: ProjectSchedule['state']): ProjectSchedule {
  return {
    scheduleId, label: scheduleId, promptText: 'p', cadence: 'daily', hourLocal: 7, timezone: 'UTC', deliverTo: 'owner',
    notifyMembers: false, publishOutputs: false, state, createdByEmail: 'me@x.edu', createdAt: '', updatedAt: '', isMine: true, canEdit: true, canManage: true,
  };
}

describe('ProjectRailComponent', () => {
  const api = {
    instructions: vi.fn(), model: vi.fn(), saveModel: vi.fn(), bindings: vi.fn(), files: vi.fn(), memory: vi.fn(), memoryEntries: vi.fn(), schedules: vi.fn(),
  };
  const agents = { loadBindable: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };

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
    api.schedules.mockReturnValue(of({ schedules: [sched('a', 'active'), sched('b', 'paused_error')] }));
    agents.loadBindable.mockResolvedValue(MODELS);
    TestBed.configureTestingModule({
      imports: [ProjectRailComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: AgentService, useValue: agents },
        { provide: ToastService, useValue: toast },
        provideRouter([]),
      ],
    });
  });

  async function settle(fixture: { detectChanges(): void; whenStable(): Promise<unknown> }) {
    fixture.detectChanges();
    await fixture.whenStable();
    await new Promise(r => setTimeout(r, 0));
    fixture.detectChanges();
  }

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectRailComponent);
    fixture.componentRef.setInput('project', project);
    await settle(fixture);
    const el = fixture.nativeElement as HTMLElement;
    const row = (label: string) =>
      Array.from(el.querySelectorAll<HTMLElement>('nav li > *')).find(r => r.querySelector('span > span')?.textContent?.trim() === label)!;
    return { fixture, el, row, component: fixture.componentInstance };
  }

  /** A row's label, meta and verb, space-separated (they are sibling spans with no whitespace between). */
  function text(node: Element | undefined): string {
    return Array.from(node?.querySelectorAll('span') ?? [])
      .filter(span => span.children.length === 0 || span.querySelector('ng-icon'))
      .map(span => span.textContent?.trim() ?? '')
      .filter(Boolean)
      .join(' ');
  }

  /** The Model menu's items, from the overlay CDK renders outside the fixture. */
  function menuItems(): HTMLButtonElement[] {
    return Array.from(document.querySelectorAll<HTMLButtonElement>('[aria-label="Project model"] [role="menuitemradio"]'));
  }

  it('summarises each setting on one line, with a verb for what the row lets you do', async () => {
    const { row, el } = await render();
    expect(text(row('Instructions'))).toBe('Instructions Not set Edit');
    expect(text(row('Files'))).toBe('Files 3 files Add');
    expect(text(row('Memory'))).toBe('Memory 2 files Open');
    expect(text(row('Model'))).toBe('Model Haiku 4.5 Change');
    expect(text(row('Tools'))).toBe('Tools 2 tools Edit');
    expect(text(row('Skills'))).toBe('Skills 1 skill Edit');
    expect(text(row('Schedules'))).toBe('Schedules 2 schedules · 1 paused Manage');
    expect(text(row('Members'))).toBe('Members 4 people Manage');
    expect(text(row('History'))).toBe('History Version 2 View');
    expect(el.textContent).toContain('Shared with 3 other people. Owned by Olive Owner. You’re an editor.');
  });

  it('links every row but Model to its own page', async () => {
    const { el, row } = await render();
    const links = Array.from(el.querySelectorAll<HTMLAnchorElement>('nav a')).map(a => a.getAttribute('href'));
    expect(links).toEqual([
      '/projects/prj_1/instructions',
      '/projects/prj_1/files',
      '/projects/prj_1/memory',
      '/projects/prj_1/tools',
      '/projects/prj_1/skills',
      '/projects/prj_1/schedules',
      '/projects/prj_1/members',
      '/projects/prj_1/activity',
      '/projects/prj_1/history',
    ]);
    expect(row('Model').tagName).toBe('BUTTON');
    expect(row('Model').getAttribute('aria-haspopup')).toBe('menu');
    expect(api.memoryEntries).toHaveBeenCalledWith('spc_1');
  });

  it('counts the skills with a newer version', async () => {
    api.bindings.mockImplementation((_id: string, kind: 'tools' | 'skills') =>
      of({
        bindings: kind === 'tools' ? [] : [{ ref: 's1', version: 2, updateAvailable: true }, { ref: 's2', version: 1, updateAvailable: false }],
        version: 2,
        canEdit: true,
      }),
    );
    const { row } = await render();
    expect(text(row('Tools'))).toBe('Tools None Edit');
    expect(text(row('Skills'))).toBe('Skills 2 skills · 1 update Edit');
  });

  it('leaves the Memory count blank when memory is unavailable', async () => {
    api.memory.mockReturnValue(throwError(() => new Error('off')));
    const { row } = await render();
    expect(text(row('Memory'))).toBe('Memory Open');
  });

  it('reads for a viewer: View links, no Activity, and a Model row that only shows the model', async () => {
    const { row, el } = await render({ ...PROJECT, role: 'viewer' });
    expect(text(row('Instructions'))).toBe('Instructions Not set View');
    expect(text(row('Members'))).toBe('Members 4 people View');
    expect(text(row('Model'))).toBe('Model Haiku 4.5');
    expect(row('Model').tagName).toBe('DIV');
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
    // Without the catalog there is nothing to choose from, so Model only shows.
    expect(text(row('Model'))).toBe('Model m-haiku');
    expect(text(row('Files'))).toBe('Files Add');
  });

  it('says None without schedules, and Not turned on where scheduled runs are off', async () => {
    api.schedules.mockReturnValue(of({ schedules: [] }));
    expect(text((await render()).row('Schedules'))).toBe('Schedules None Manage');
    api.schedules.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    expect(text((await render()).row('Schedules'))).toBe('Schedules Not turned on Manage');
    api.schedules.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    expect(text((await render({ ...PROJECT, role: 'viewer' })).row('Schedules'))).toBe('Schedules View');
  });

  describe('the Model menu', () => {
    it('lists the models with the project’s checked', async () => {
      const { row, fixture } = await render();
      row('Model').click();
      await settle(fixture);
      const items = menuItems();
      expect(items.map(i => Array.from(i.querySelectorAll('span span'), s => s.textContent?.trim()))).toEqual([['Haiku 4.5', 'Fast'], ['Sonnet 4.6']]);
      expect(items.map(i => i.getAttribute('aria-checked'))).toEqual(['true', 'false']);
      expect(document.querySelector('[aria-label="Project model"]')?.textContent).toContain('Members who can’t use it get their own default');
      fixture.destroy();
    });

    it('saves a pick straight away, and the row and History follow', async () => {
      api.saveModel.mockReturnValue(of({ modelConfig: { modelId: 'm-sonnet', provider: 'bedrock' }, version: 3, canEdit: true }));
      const { row, fixture, component } = await render();
      row('Model').click();
      await settle(fixture);
      menuItems()[1].click();
      await settle(fixture);
      expect(api.saveModel).toHaveBeenCalledWith('prj_1', { modelId: 'm-sonnet', provider: 'bedrock' });
      expect(toast.success).toHaveBeenCalledWith('The project now runs on Sonnet 4.6');
      expect(text(row('Model'))).toBe('Model Sonnet 4.6 Change');
      expect(text(row('History'))).toBe('History Version 3 View');
      expect(component.modelLabel()).toBe('Sonnet 4.6');
      fixture.destroy();
    });

    it('does nothing when the project’s own model is picked', async () => {
      const { row, fixture } = await render();
      row('Model').click();
      await settle(fixture);
      menuItems()[0].click();
      await settle(fixture);
      expect(api.saveModel).not.toHaveBeenCalled();
      fixture.destroy();
    });

    it('keeps the old model and says so when the save is refused', async () => {
      api.saveModel.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'You can’t use that model.' } })));
      const { row, fixture } = await render();
      row('Model').click();
      await settle(fixture);
      menuItems()[1].click();
      await settle(fixture);
      expect(toast.error).toHaveBeenCalledWith('The model could not be saved', 'You can’t use that model.');
      expect(text(row('Model'))).toBe('Model Haiku 4.5 Change');
      fixture.destroy();
    });

    it('explains a project model the caller cannot use', async () => {
      api.model.mockReturnValue(of({ modelConfig: { modelId: 'm-secret' }, version: 2, canEdit: true }));
      const { row, fixture } = await render();
      row('Model').click();
      await settle(fixture);
      expect(document.querySelector('[aria-label="Project model"]')?.textContent).toContain('which isn’t a model you can use');
      expect(menuItems().every(i => i.getAttribute('aria-checked') === 'false')).toBe(true);
      fixture.destroy();
    });

    it('is not offered on an archived project', async () => {
      const { row } = await render({ ...PROJECT, status: 'archived' });
      expect(row('Model').tagName).toBe('DIV');
      expect(text(row('Model'))).toBe('Model Haiku 4.5');
    });
  });
});
