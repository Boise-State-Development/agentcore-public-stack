import { describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
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

function render(folder: FolderAttachmentData = FOLDER, flag = true, refresh = vi.fn().mockResolvedValue({})) {
  TestBed.resetTestingModule();
  const toast = { info: vi.fn(), error: vi.fn() };
  TestBed.configureTestingModule({
    providers: [
      { provide: FEATURES, useValue: { folderAttachments: flag } },
      { provide: FolderRefreshService, useValue: { refresh } },
      { provide: ToastService, useValue: toast },
    ],
  });
  const fixture = TestBed.createComponent(FolderAttachmentChipComponent);
  fixture.componentRef.setInput('folder', folder);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  return { fixture, el, button: el.querySelector('button') as HTMLButtonElement, toast, refresh };
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
});
