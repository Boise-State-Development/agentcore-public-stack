import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BindableItem } from '../../agents/models/agent.model';
import { AgentService } from '../../agents/services/agent.service';
import { ToastService } from '../../services/toast/toast.service';
import { BoundBinding, Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { TEST_PROJECT, buttonNamed, openProjectPage, provideProjectPageRoute, settlePage } from '../../../testing/project-page';
import { ProjectBindingsPage } from './project-bindings.page';

function bindable(ref: string, label: string, description: string, category?: string): BindableItem {
  return { kind: 'tool', ref, label, description, meta: category ? { category } : {} };
}

const TOOLS = [
  bindable('calendar', 'Calendar', 'Read your schedule', 'account'),
  bindable('browse_web', 'Web Browser', 'Open and read pages', 'browser'),
];
const SKILLS = [bindable('pdf', 'PDF Workflows', 'Fills PDFs'), bindable('memo', 'Memo Style', 'Writes memos')];

describe('ProjectBindingsPage', () => {
  const api = {
    get: vi.fn(),
    bindings: vi.fn(),
    saveBindings: vi.fn((_id: string, _kind: string, bindings: { ref: string }[]) => of({ bindings, version: 3, canEdit: true })),
    pinSkill: vi.fn(),
  };
  const agents = { loadBindable: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(TEST_PROJECT));
    api.bindings.mockReturnValue(of({ bindings: [{ ref: 'calendar' }, { ref: 'gone' }], version: 2, canEdit: true }));
    agents.loadBindable.mockImplementation((kind: string) => Promise.resolve(kind === 'tool' ? TOOLS : SKILLS));
    TestBed.configureTestingModule({
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: AgentService, useValue: agents },
        { provide: ToastService, useValue: toast },
      ],
    });
  });

  function route(kind: 'tools' | 'skills') {
    TestBed.configureTestingModule({ providers: [provideProjectPageRoute(kind, ProjectBindingsPage, { data: { kind } })] });
  }

  async function openTools(project: Project = TEST_PROJECT) {
    api.get.mockReturnValue(of(project));
    route('tools');
    return openProjectPage('/projects/prj_1/tools');
  }

  /** The skills page with these pins; every pinned ref is bound. */
  async function openSkills(pins: Record<string, BoundBinding>, project: Project = TEST_PROJECT) {
    api.get.mockReturnValue(of(project));
    api.bindings.mockReturnValue(of({ bindings: Object.values(pins), version: 2, canEdit: true }));
    route('skills');
    return openProjectPage('/projects/prj_1/skills');
  }

  const nameNode = (container: HTMLElement, name: string): Element =>
    [...container.querySelectorAll('[id$="-name"]')].find(n => n.textContent!.trim() === name)!;
  const checkboxFor = (container: HTMLElement, name: string): HTMLInputElement =>
    container.querySelector(`input[aria-labelledby="${nameNode(container, name).id}"]`) as HTMLInputElement;
  const rowFor = (container: HTMLElement, name: string): HTMLLIElement => nameNode(container, name).closest('li')!;
  const rowText = (container: HTMLElement, name: string): string => rowFor(container, name).textContent!.replace(/\s+/g, ' ').trim();

  it('reads the kind from the route: its title, its bindings and its palette', async () => {
    const tools = await openTools();
    expect(tools.el.querySelector('h1')?.textContent).toContain('Tools');
    expect(tools.el.querySelector('[aria-current="page"]')?.textContent).toContain('Tools');
    expect(tools.el.textContent).toContain('Members without access to a tool get the assistant without it.');
    expect(tools.el.querySelector('input[type="search"]')?.getAttribute('placeholder')).toBe('Search tools…');
    expect(api.bindings).toHaveBeenCalledWith('prj_1', 'tools');
    expect(agents.loadBindable).toHaveBeenCalledWith('tool');

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: AgentService, useValue: agents },
        { provide: ToastService, useValue: toast },
      ],
    });
    const skills = await openSkills({});
    expect(skills.el.querySelector('h1')?.textContent).toContain('Skills');
    expect(skills.el.textContent).toContain('stays on the version it was added at');
    expect(agents.loadBindable).toHaveBeenLastCalledWith('skill');
  });

  it('keeps a binding the caller cannot use, locked and explained', async () => {
    const { el } = await openTools();
    const gone = checkboxFor(el, 'gone');
    expect(gone.checked).toBe(true);
    expect(gone.disabled).toBe(true);
    expect(el.textContent).toContain('Added by someone else.');
  });

  it('saves its own kind in palette order, keeping the locked binding, and stays on the page', async () => {
    const { el, harness } = await openTools();
    // In the header beside the title, not below it.
    const save = buttonNamed(el.querySelector('[data-shell-actions]')!, 'Save tools')!;
    expect(save.disabled).toBe(true);

    checkboxFor(el, 'Web Browser').click();
    await settlePage(harness);
    expect(save.disabled).toBe(false);
    expect(harness.routeDebugElement!.componentInstance.hasUnsavedChanges()).toBe(true);

    save.click();
    await settlePage(harness);
    expect(api.saveBindings).toHaveBeenCalledWith('prj_1', 'tools', [{ ref: 'calendar' }, { ref: 'browse_web' }, { ref: 'gone' }]);
    expect(toast.success).toHaveBeenCalledWith('Tools saved');
    expect(save.disabled).toBe(true);
    expect(harness.routeDebugElement!.componentInstance.hasUnsavedChanges()).toBe(false);
  });

  it('discards a change back to what is saved', async () => {
    const { el, harness } = await openTools();
    checkboxFor(el, 'Web Browser').click();
    await settlePage(harness);
    buttonNamed(el, 'Discard changes')!.click();
    await settlePage(harness);
    expect(checkboxFor(el, 'Web Browser').checked).toBe(false);
    expect(buttonNamed(el, 'Save tools')!.disabled).toBe(true);
  });

  it('is read-only for a viewer, and on an archived project', async () => {
    for (const project of [{ ...TEST_PROJECT, role: 'viewer' as const }, { ...TEST_PROJECT, status: 'archived' as const }]) {
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: [
          { provide: ProjectApiService, useValue: api },
          { provide: AgentService, useValue: agents },
          { provide: ToastService, useValue: toast },
        ],
      });
      const { el } = await openTools(project);
      const boxes = [...el.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
      expect(boxes.length).toBe(3);
      expect(boxes.every(b => b.disabled)).toBe(true);
      expect(buttonNamed(el, 'Save tools')).toBeUndefined();
      expect(buttonNamed(el, 'Select all')).toBeUndefined();
    }
  });

  it('says so when the bindings cannot be read', async () => {
    api.bindings.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    const { el } = await openTools();
    expect(el.querySelector('[role=alert]')?.textContent).toBeTruthy();
    expect(buttonNamed(el, 'Save tools')).toBeUndefined();
  });

  describe('skill pins', () => {
    it('shows the version each bound skill runs, and nothing on one that is not bound', async () => {
      const { el } = await openSkills({
        pdf: { ref: 'pdf', version: 2, pinnedAt: '2026-10-01T12:00:00Z', updateAvailable: false },
      });
      expect(rowText(el, 'PDF Workflows')).toContain('Version 2 · since Oct 1, 2026.');
      expect(rowFor(el, 'PDF Workflows').querySelector('button')).toBeNull();
      expect(rowText(el, 'Memo Style')).not.toContain('Version');
    });

    it('offers Update when the skill has changed, and saves the new pin straight away', async () => {
      api.pinSkill.mockReturnValue(
        of({ bindings: [{ ref: 'pdf', version: 3, pinnedAt: '2026-10-08T12:00:00Z', updateAvailable: false }], version: 7, canEdit: true }),
      );
      const { el, harness } = await openSkills({
        pdf: { ref: 'pdf', version: 2, pinnedAt: '2026-10-01T12:00:00Z', updateAvailable: true },
      });
      expect(rowText(el, 'PDF Workflows')).toContain('A newer version is available');
      const update = rowFor(el, 'PDF Workflows').querySelector('button')!;
      expect(update.textContent!.trim()).toBe('Update');
      expect(update.getAttribute('aria-label')).toBe('Update PDF Workflows');

      update.click();
      await settlePage(harness);

      expect(api.pinSkill).toHaveBeenCalledWith('prj_1', 'pdf');
      expect(rowText(el, 'PDF Workflows')).toContain('Version 3 · since Oct 8, 2026.');
      expect(rowFor(el, 'PDF Workflows').querySelector('button')).toBeNull();
      expect(toast.success).toHaveBeenCalledWith('PDF Workflows updated');
      // Updating doesn't toggle the row, so Save stays off.
      expect(checkboxFor(el, 'PDF Workflows').checked).toBe(true);
      expect(buttonNamed(el, 'Save skills')!.disabled).toBe(true);
    });

    it('lets an editor pin a skill bound before pins, which runs the latest version', async () => {
      const { el } = await openSkills({ pdf: { ref: 'pdf', version: null, pinnedAt: null, updateAvailable: false } });
      expect(rowText(el, 'PDF Workflows')).toContain('Uses the latest version.');
      const pin = rowFor(el, 'PDF Workflows').querySelector('button')!;
      expect(pin.textContent!.trim()).toBe('Pin this version');
      expect(pin.getAttribute('aria-label')).toBe('Pin the current version of PDF Workflows');
    });

    it('offers no Update to a viewer', async () => {
      const { el } = await openSkills(
        { pdf: { ref: 'pdf', version: 2, pinnedAt: null, updateAvailable: true } },
        { ...TEST_PROJECT, role: 'viewer' },
      );
      expect(rowText(el, 'PDF Workflows')).toContain('A newer version is available');
      expect(rowFor(el, 'PDF Workflows').querySelector('button')).toBeNull();
    });

    it('says why an update failed and keeps the old pin', async () => {
      api.pinSkill.mockReturnValue(
        throwError(
          () => new HttpErrorResponse({ status: 409, error: { detail: 'The skill changed while it was being pinned. Try again.' } }),
        ),
      );
      const { el, harness } = await openSkills({ pdf: { ref: 'pdf', version: 2, pinnedAt: null, updateAvailable: true } });
      rowFor(el, 'PDF Workflows').querySelector('button')!.click();
      await settlePage(harness);
      expect(el.querySelector('[role=alert]')?.textContent).toContain('The skill changed while it was being pinned');
      expect(rowText(el, 'PDF Workflows')).toContain('Version 2');
    });
  });
});
