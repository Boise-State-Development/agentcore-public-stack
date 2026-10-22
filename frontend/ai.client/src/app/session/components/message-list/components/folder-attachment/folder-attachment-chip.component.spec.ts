import { describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { SaveFolderAsProjectDialogComponent } from '../../../../../projects/components/save-folder-as-project-dialog.component';
import { FEATURES } from '../../../../../services/features';
import { FolderAttachmentError, FolderRefreshService } from '../../../../../services/folder-attachments';
import { ToastService } from '../../../../../services/toast/toast.service';
import { FolderAttachmentChipComponent } from './folder-attachment-chip.component';
import type { FolderAttachmentData } from '../../../../services/models/message.model';

const FOLDER: FolderAttachmentData = {
  name: 'BIO 101',
  source: 'Google Drive',
  status: 'ready',
  fileCount: 3,
  files: ['syllabus.pdf', 'week 1/rubric.docx'],
  moreCount: 1,
};

function render(
  folder: FolderAttachmentData = FOLDER,
  flag = true,
  refresh = vi.fn().mockResolvedValue({}),
  opts: { projects?: boolean; dialogResult?: unknown } = {},
) {
  TestBed.resetTestingModule();
  const toast = { info: vi.fn(), error: vi.fn(), success: vi.fn() };
  const dialogOpen = vi.fn((_component: unknown, _config?: unknown) => ({ closed: of(opts.dialogResult) }));
  const navigate = vi.fn().mockResolvedValue(true);
  TestBed.configureTestingModule({
    providers: [
      { provide: FEATURES, useValue: { folderAttachments: flag, projects: opts.projects ?? false } },
      { provide: FolderRefreshService, useValue: { refresh } },
      { provide: ToastService, useValue: toast },
      { provide: Dialog, useValue: { open: dialogOpen } },
      { provide: Router, useValue: { navigate } },
    ],
  });
  const fixture = TestBed.createComponent(FolderAttachmentChipComponent);
  fixture.componentRef.setInput('folder', folder);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  return { fixture, el, button: el.querySelector('button') as HTMLButtonElement, toast, refresh, dialogOpen, navigate };
}

describe('FolderAttachmentChipComponent', () => {
  it('shows the name, the count and the source', () => {
    const { el } = render();
    expect(el.textContent).toContain('BIO 101');
    expect(el.textContent).toContain('3 files · Google Drive');
  });

  it('is a disclosure: collapsed, then the file list it controls', () => {
    const { fixture, el, button } = render();
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(el.querySelector('ul')).toBeNull();
    expect(button.textContent).toContain('Show the files in BIO 101');

    button.click();
    fixture.detectChanges();
    const list = el.querySelector('ul')!;
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(button.getAttribute('aria-controls')).toBe(list.id);
    expect([...list.querySelectorAll('li')].map((li) => li.textContent?.trim())).toEqual([
      'syllabus.pdf',
      'week 1/rubric.docx',
      '… and 1 more',
    ]);
  });

  it('says so while the folder was still being added', () => {
    const { el } = render({ ...FOLDER, status: 'expanding', fileCount: 1, source: null, moreCount: 0 });
    expect(el.textContent).toContain('1 file so far');
  });
});


describe('FolderAttachmentChipComponent — Refresh', () => {
  const refreshButton = (el: HTMLElement) => el.querySelector('[data-testid="folder-chip-refresh"]') as HTMLButtonElement | null;

  it('is offered only with the folder feature on, and named for the folder', () => {
    expect(refreshButton(render(FOLDER, false).el)).toBeNull();
    const button = refreshButton(render().el)!;
    expect(button.getAttribute('aria-label')).toBe('Refresh BIO 101');
  });

  it('refreshes this folder and says it goes with the next message', async () => {
    const { el, refresh, toast } = render();
    refreshButton(el)!.click();
    await vi.waitFor(() => expect(toast.info).toHaveBeenCalled());
    expect(refresh).toHaveBeenCalledWith(FOLDER);
    expect(toast.info.mock.calls[0][1]).toContain('next message');
  });

  it('explains a 409', async () => {
    const { el, toast } = render(FOLDER, true, vi.fn().mockRejectedValue(new FolderAttachmentError('x', 409)));
    refreshButton(el)!.click();
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][1]).toContain('reconnecting');
  });

  describe('Save as project', () => {
    const save = (el: HTMLElement) => el.querySelector<HTMLButtonElement>('[data-testid="folder-chip-save-project"]');
    const PROJECT = { projectId: 'p-1', name: 'BIO 101' };

    it('is offered only with both folders and projects on', () => {
      expect(save(render(FOLDER, true, undefined, { projects: true }).el)).not.toBeNull();
      expect(save(render(FOLDER, true, undefined, { projects: false }).el)).toBeNull();
      expect(save(render(FOLDER, false, undefined, { projects: true }).el)).toBeNull();
    });

    it('is named for the folder', () => {
      const { el } = render(FOLDER, true, undefined, { projects: true });
      expect(save(el)!.getAttribute('aria-label')).toBe('Save BIO 101 as a project');
    });

    it("opens the dialog with the chip's folder, then offers the project from a toast", async () => {
      const { el, dialogOpen, toast, navigate } = render(FOLDER, true, undefined, {
        projects: true,
        dialogResult: { project: PROJECT, imported: 3, notImported: 0, error: null },
      });
      save(el)!.click();
      await vi.waitFor(() => expect(toast.success).toHaveBeenCalled());
      expect(dialogOpen.mock.calls[0][0]).toBe(SaveFolderAsProjectDialogComponent);
      expect(dialogOpen.mock.calls[0][1]).toEqual({ data: { folder: FOLDER } });
      const [title, message, options] = toast.success.mock.calls[0];
      expect(title).toBe('Saved as project');
      expect(message).toBe('BIO 101: 3 files are being added to its Files.');
      expect(navigate).not.toHaveBeenCalled();
      options.action.handler();
      expect(navigate).toHaveBeenCalledWith(['/projects', 'p-1']);
    });

    it('opens the project straight away after a partial save the person chose to open', async () => {
      const { el, toast, navigate } = render(FOLDER, true, undefined, {
        projects: true,
        dialogResult: { project: PROJECT, imported: 1, notImported: 2, error: 'Reconnect' },
      });
      save(el)!.click();
      await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith(['/projects', 'p-1']));
      expect(toast.success).not.toHaveBeenCalled();
    });

    it('does nothing when the dialog is cancelled', async () => {
      const { el, toast, navigate, dialogOpen } = render(FOLDER, true, undefined, { projects: true });
      save(el)!.click();
      await vi.waitFor(() => expect(dialogOpen).toHaveBeenCalled());
      await Promise.resolve();
      expect(toast.success).not.toHaveBeenCalled();
      expect(navigate).not.toHaveBeenCalled();
    });
  });
});
