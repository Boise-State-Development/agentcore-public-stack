import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BindableItem } from '../../agents/models/agent.model';
import { ToastService } from '../../services/toast/toast.service';
import { BoundBinding } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  ProjectBindingsDialogComponent,
  ProjectBindingsDialogData,
  ProjectBindingsDialogResult,
} from './project-bindings-dialog.component';

function bindable(ref: string, label: string, description: string, category?: string): BindableItem {
  return { kind: 'tool', ref, label, description, meta: category ? { category } : {} };
}

const TOOLS = [
  bindable('calendar', 'Calendar', 'Read your schedule', 'account'),
  bindable('browse_web', 'Web Browser', 'Open and read pages', 'browser'),
];
const SKILLS = [bindable('pdf', 'PDF Workflows', 'Fills PDFs'), bindable('memo', 'Memo Style', 'Writes memos')];

describe('ProjectBindingsDialogComponent', () => {
  const api = {
    saveBindings: vi.fn((_id: string, _kind: string, bindings: { ref: string }[]) =>
      of({ bindings, version: 3, canEdit: true }),
    ),
    pinSkill: vi.fn(),
  };
  const toast = { success: vi.fn(), error: vi.fn() };

  beforeEach(() => {
    vi.clearAllMocks();
    TestBed.configureTestingModule({
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(data: Partial<ProjectBindingsDialogData> = {}) {
    const opened = await openInCdkDialog<
      ProjectBindingsDialogComponent,
      ProjectBindingsDialogData,
      ProjectBindingsDialogResult
    >(ProjectBindingsDialogComponent, {
      data: {
        projectId: 'p1',
        canEdit: true,
        kind: 'tools',
        bound: ['calendar', 'gone'],
        palette: TOOLS,
        ...data,
      },
    });
    const closed = vi.fn();
    opened.ref.closed.subscribe(closed);
    return { ...opened, closed };
  }

  /** The skills dialog with these pins; every pinned ref is bound. */
  function openSkills(pins: Record<string, BoundBinding>, data: Partial<ProjectBindingsDialogData> = {}) {
    return open({ kind: 'skills', bound: Object.keys(pins), palette: SKILLS, pins, ...data });
  }

  const nameNode = (container: HTMLElement, name: string): Element =>
    [...container.querySelectorAll('[id$="-name"]')].find((n) => n.textContent!.trim() === name)!;
  const checkboxFor = (container: HTMLElement, name: string): HTMLInputElement =>
    container.querySelector(`input[aria-labelledby="${nameNode(container, name).id}"]`) as HTMLInputElement;
  const rowFor = (container: HTMLElement, name: string): HTMLLIElement => nameNode(container, name).closest('li')!;
  const rowText = (container: HTMLElement, name: string): string =>
    rowFor(container, name).textContent!.replace(/\s+/g, ' ').trim();
  const button = (container: HTMLElement, text: string): HTMLButtonElement =>
    [...container.querySelectorAll('button')].find((b) => b.textContent!.trim() === text) as HTMLButtonElement;
  const settle = async () => {
    TestBed.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    TestBed.tick();
  };

  it('is a named dialog per kind, with one shared selector', async () => {
    const tools = await open();
    expectNamedDialog(tools.container, { name: 'Tools' });
    expect(tools.container.querySelectorAll('app-tool-selector')).toHaveLength(1);
    expect(tools.container.textContent).toContain('Members without access to a tool get the assistant without it.');
    expect(tools.container.querySelector('input[type="search"]')?.getAttribute('placeholder')).toBe('Search tools…');
    TestBed.inject(Dialog).closeAll();

    const skills = await openSkills({});
    expectNamedDialog(skills.container, { name: 'Skills' });
    expect(skills.container.textContent).toContain('stays on the version it was added at');
    expect(skills.container.querySelector('input[type="search"]')?.getAttribute('placeholder')).toBe('Search skills…');
  });

  it('keeps a binding the caller cannot use, locked and explained', async () => {
    const { container } = await open();
    const gone = checkboxFor(container, 'gone');
    expect(gone.checked).toBe(true);
    expect(gone.disabled).toBe(true);
    expect(container.textContent).toContain('Added by someone else.');
  });

  it('saves its own kind in palette order, keeping the locked binding, and hands back the response', async () => {
    const { container, closed } = await open();

    checkboxFor(container, 'Web Browser').click();
    await settle();
    button(container, 'Save').click();
    await settle();

    expect(api.saveBindings).toHaveBeenCalledTimes(1);
    expect(api.saveBindings).toHaveBeenCalledWith('p1', 'tools', [
      { ref: 'calendar' },
      { ref: 'browse_web' },
      { ref: 'gone' },
    ]);
    expect(toast.success).toHaveBeenCalledWith('Tools saved');
    expect(closed).toHaveBeenCalledWith(expect.objectContaining({ version: 3 }));
  });

  it('keeps Save disabled until something changes, and Cancel reports nothing', async () => {
    const { container, closed } = await open();
    expect(button(container, 'Save').disabled).toBe(true);
    checkboxFor(container, 'Web Browser').click();
    await settle();
    expect(button(container, 'Save').disabled).toBe(false);
    button(container, 'Cancel').click();
    await settle();
    expect(closed).toHaveBeenCalledWith(undefined);
  });

  it('is read-only for a viewer', async () => {
    const { container } = await open({ canEdit: false });
    const boxes = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes.length).toBe(3);
    expect(boxes.every((b) => b.disabled)).toBe(true);
    expect(button(container, 'Save')).toBeUndefined();
    expect(button(container, 'Select all')).toBeUndefined();
    expect(button(container, 'Close')).toBeDefined();
  });

  describe('skill pins', () => {
    it('shows the version each bound skill runs, and nothing on one that is not bound', async () => {
      const { container } = await openSkills({
        pdf: { ref: 'pdf', version: 2, pinnedAt: '2026-10-01T12:00:00Z', updateAvailable: false },
      });
      expect(rowText(container, 'PDF Workflows')).toContain('Version 2 · since Oct 1, 2026.');
      expect(rowFor(container, 'PDF Workflows').querySelector('button')).toBeNull();
      expect(rowText(container, 'Memo Style')).not.toContain('Version');
      // The version line describes the row's checkbox.
      const box = checkboxFor(container, 'PDF Workflows');
      const described = box.getAttribute('aria-describedby')!.split(' ').map((id) => container.querySelector(`#${id}`)!.textContent);
      expect(described[0]).toContain('Version 2');
    });

    it('offers Update when the skill has changed, and saves the new pin straight away', async () => {
      api.pinSkill.mockReturnValue(
        of({ bindings: [{ ref: 'pdf', version: 3, pinnedAt: '2026-10-08T12:00:00Z', updateAvailable: false }], version: 7, canEdit: true }),
      );
      const { container, closed } = await openSkills({
        pdf: { ref: 'pdf', version: 2, pinnedAt: '2026-10-01T12:00:00Z', updateAvailable: true },
      });
      expect(rowText(container, 'PDF Workflows')).toContain('A newer version is available');
      const update = rowFor(container, 'PDF Workflows').querySelector('button')!;
      expect(update.textContent!.trim()).toBe('Update');
      expect(update.getAttribute('aria-label')).toBe('Update PDF Workflows');

      update.click();
      await settle();

      expect(api.pinSkill).toHaveBeenCalledWith('p1', 'pdf');
      expect(rowText(container, 'PDF Workflows')).toContain('Version 3 · since Oct 8, 2026.');
      expect(rowFor(container, 'PDF Workflows').querySelector('button')).toBeNull();
      expect(toast.success).toHaveBeenCalledWith('PDF Workflows updated');
      // Updating doesn't toggle the row, so Save stays off.
      expect(checkboxFor(container, 'PDF Workflows').checked).toBe(true);
      expect(button(container, 'Save').disabled).toBe(true);

      // Closing reports the saved pin, so the rail's history version moves.
      button(container, 'Cancel').click();
      await settle();
      expect(closed).toHaveBeenCalledWith(expect.objectContaining({ version: 7 }));
    });

    it('lets an editor pin a skill bound before pins, which runs the latest version', async () => {
      const { container } = await openSkills({ pdf: { ref: 'pdf', version: null, pinnedAt: null, updateAvailable: false } });
      expect(rowText(container, 'PDF Workflows')).toContain('Uses the latest version.');
      const pin = rowFor(container, 'PDF Workflows').querySelector('button')!;
      expect(pin.textContent!.trim()).toBe('Pin this version');
      expect(pin.getAttribute('aria-label')).toBe('Pin the current version of PDF Workflows');
    });

    it('offers no Update to a viewer', async () => {
      const { container } = await openSkills(
        { pdf: { ref: 'pdf', version: 2, pinnedAt: null, updateAvailable: true } },
        { canEdit: false },
      );
      expect(rowText(container, 'PDF Workflows')).toContain('A newer version is available');
      expect(rowFor(container, 'PDF Workflows').querySelector('button')).toBeNull();
    });

    it('says why an update failed and keeps the old pin', async () => {
      api.pinSkill.mockReturnValue(
        throwError(
          () => new HttpErrorResponse({ status: 409, error: { detail: 'The skill changed while it was being pinned. Try again.' } }),
        ),
      );
      const { container, closed } = await openSkills({ pdf: { ref: 'pdf', version: 2, pinnedAt: null, updateAvailable: true } });
      rowFor(container, 'PDF Workflows').querySelector('button')!.click();
      await settle();
      expect(container.querySelector('[role=alert]')?.textContent).toContain('The skill changed while it was being pinned');
      expect(rowText(container, 'PDF Workflows')).toContain('Version 2');
      button(container, 'Cancel').click();
      await settle();
      expect(closed).toHaveBeenCalledWith(undefined);
    });
  });
});
