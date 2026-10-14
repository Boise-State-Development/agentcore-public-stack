import { describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
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

function render(folder: FolderAttachmentData = FOLDER) {
  TestBed.resetTestingModule();
  const fixture = TestBed.createComponent(FolderAttachmentChipComponent);
  fixture.componentRef.setInput('folder', folder);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  return { fixture, el, button: el.querySelector('button') as HTMLButtonElement };
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
