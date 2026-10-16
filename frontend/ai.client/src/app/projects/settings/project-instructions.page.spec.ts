import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ToastService } from '../../services/toast/toast.service';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { TEST_PROJECT, buttonNamed, openProjectPage, provideProjectPageRoute, settlePage } from '../../../testing/project-page';
import { ProjectInstructionsPage } from './project-instructions.page';
import { unsavedChangesGuard } from './unsaved-changes.guard';

describe('ProjectInstructionsPage', () => {
  const api = { get: vi.fn(), instructions: vi.fn(), saveInstructions: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(TEST_PROJECT));
    api.instructions.mockReturnValue(of({ instructions: 'Be terse.', version: 4, canEdit: true }));
    api.saveInstructions.mockImplementation((_id: string, text: string) => of({ instructions: text, version: 5, canEdit: true }));
    dialog.open.mockReturnValue({ closed: of(false) });
    TestBed.configureTestingModule({
      providers: [
        provideProjectPageRoute('instructions', ProjectInstructionsPage, { canDeactivate: [unsavedChangesGuard] }),
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function open(project: Project = TEST_PROJECT) {
    api.get.mockReturnValue(of(project));
    const page = await openProjectPage('/projects/prj_1/instructions');
    const textarea = page.el.querySelector<HTMLTextAreaElement>('textarea')!;
    const type = async (text: string) => {
      textarea.value = text;
      textarea.dispatchEvent(new Event('input'));
      await settlePage(page.harness);
    };
    return { ...page, textarea, type };
  }

  it('shows the project’s instructions under a breadcrumb back to the project', async () => {
    const { el, textarea } = await open();
    expect(api.instructions).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h1')?.textContent).toContain('Instructions');
    const crumbs = Array.from(el.querySelectorAll('nav[aria-label="Breadcrumb"] a')).map(a => a.getAttribute('href'));
    expect(crumbs).toEqual(['/projects', '/projects/prj_1/overview']);
    expect(textarea.value).toBe('Be terse.');
    expect(textarea.disabled).toBe(false);
    expect(el.textContent).toContain('9 / 100,000 characters');
    const version = Array.from(el.querySelectorAll('a')).find(a => a.textContent?.trim() === 'Version 4')!;
    expect(version.getAttribute('href')).toBe('/projects/prj_1/history?version=4');
  });

  it('saves a change as a new version and stays on the page', async () => {
    const { el, harness, type } = await open();
    // In the header beside the title, not below it.
    const save = buttonNamed(el.querySelector('[data-shell-actions]')!, 'Save instructions')!;
    expect(save.disabled).toBe(true);
    await type('Be terse. Cite files.');
    expect(save.disabled).toBe(false);

    save.click();
    await settlePage(harness);
    expect(api.saveInstructions).toHaveBeenCalledWith('prj_1', 'Be terse. Cite files.');
    expect(toast.success).toHaveBeenCalledWith('Instructions saved');
    expect(save.disabled).toBe(true);
    expect(el.textContent).toContain('Version 5');
    expect(harness.routeDebugElement!.componentInstance.hasUnsavedChanges()).toBe(false);
  });

  it('saves from the keyboard', async () => {
    const { textarea, harness, type } = await open();
    await type('Be brief.');
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 's', metaKey: true, bubbles: true }));
    await settlePage(harness);
    expect(api.saveInstructions).toHaveBeenCalledWith('prj_1', 'Be brief.');
  });

  it('keeps the draft and says why when a save fails', async () => {
    api.saveInstructions.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Someone saved first.' } })));
    const { el, harness, textarea, type } = await open();
    await type('Mine.');
    buttonNamed(el, 'Save instructions')!.click();
    await settlePage(harness);
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Someone saved first.');
    expect(textarea.value).toBe('Mine.');
  });

  it('discards a draft back to what is saved', async () => {
    const { el, harness, textarea, type } = await open();
    await type('Something else.');
    buttonNamed(el, 'Discard changes')!.click();
    await settlePage(harness);
    expect(textarea.value).toBe('Be terse.');
    expect(buttonNamed(el, 'Save instructions')!.disabled).toBe(true);
  });

  it('asks before a link leaves an unsaved draft, and stays when told to', async () => {
    const { type } = await open();
    await type('Unsaved.');
    const router = TestBed.inject(Router);
    expect(await router.navigateByUrl('/elsewhere')).toBe(false);
    expect(dialog.open).toHaveBeenCalledWith(expect.anything(), {
      data: expect.objectContaining({ title: 'Discard your changes?', confirmText: 'Discard changes' }),
    });
    expect(router.url).toBe('/projects/prj_1/instructions');

    dialog.open.mockReturnValue({ closed: of(true) });
    expect(await router.navigateByUrl('/elsewhere')).toBe(true);
  });

  it('leaves without asking when nothing changed', async () => {
    await open();
    expect(await TestBed.inject(Router).navigateByUrl('/elsewhere')).toBe(true);
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('is read-only for a viewer', async () => {
    const { el, textarea } = await open({ ...TEST_PROJECT, role: 'viewer' });
    expect(textarea.disabled).toBe(true);
    expect(buttonNamed(el, 'Save instructions')).toBeUndefined();
  });

  it('is read-only on an archived project, and says so', async () => {
    const { el, textarea } = await open({ ...TEST_PROJECT, status: 'archived' });
    expect(textarea.disabled).toBe(true);
    expect(buttonNamed(el, 'Save instructions')).toBeUndefined();
    expect(el.querySelector('[role=status]')?.textContent).toContain('archived');
  });

  it('tells a non-member the project is not there', async () => {
    api.get.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const { el } = await openProjectPage('/projects/prj_1/instructions');
    expect(el.querySelector('[role=alert]')?.textContent).toContain('doesn’t exist, or you’re not a member');
    expect(el.querySelector('textarea')).toBeNull();
  });
});
