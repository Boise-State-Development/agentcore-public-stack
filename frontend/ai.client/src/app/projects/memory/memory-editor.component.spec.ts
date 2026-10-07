import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { MemoryEditorComponent, MemoryEditorMode } from './memory-editor.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { MemoryEntry, MemoryFile } from '../models/project.model';
import { MEMORY_LIMITS, memoryEntry } from '../../../testing/project-memory.fixtures';

const FILE: MemoryFile = {
  slug: 'sis',
  description: 'Banner conventions',
  version: 3,
  tokens: 50,
  items: [
    { anchor: 'aaaaaaaa', text: 'Term codes are YYYYTT.', pinned: true },
    { anchor: 'bbbbbbbb', text: 'Old link to [[gone]].', pinned: false },
    { anchor: 'cccccccc', text: 'Section IDs are CRN plus term.', pinned: false },
  ],
  people: {},
};

const ENTRIES: MemoryEntry[] = [
  memoryEntry('sis', { version: 3, aliases: ['banner'] }),
  memoryEntry('rates', { aliases: ['throttling'] }),
];

describe('MemoryEditorComponent', () => {
  const api = { memoryFile: vi.fn(), saveMemoryFile: vi.fn(), proposeMemoryChange: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.memoryFile.mockReturnValue(of(FILE));
    api.saveMemoryFile.mockReturnValue(of({ slug: 'sis', version: 4, tokens: 60, itemCount: 3, warnings: [], overSoftThreshold: false, removedAnchors: [], indexed: null }));
    api.proposeMemoryChange.mockReturnValue(of({ proposalId: 'p1' }));
    dialog.open.mockReturnValue({ closed: of('rates') });
    TestBed.configureTestingModule({
      imports: [MemoryEditorComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function render(mode: MemoryEditorMode = 'edit', scope: 'project' | 'mine' = 'project') {
    const fixture = TestBed.createComponent(MemoryEditorComponent);
    const ref = fixture.componentRef;
    ref.setInput('projectId', 'prj_1');
    ref.setInput('scope', scope);
    ref.setInput('mode', mode);
    ref.setInput('entry', mode === 'edit' || mode === 'propose' ? ENTRIES[0] : null);
    ref.setInput('entries', ENTRIES);
    ref.setInput('limits', MEMORY_LIMITS);
    const done = vi.fn();
    fixture.componentInstance.done.subscribe(done);
    const settle = async () => {
      for (let i = 0; i < 4; i++) {
        await fixture.whenStable();
        await new Promise(r => setTimeout(r, 0));
        fixture.detectChanges();
      }
    };
    fixture.detectChanges();
    await settle();
    const el = fixture.nativeElement as HTMLElement;
    const areas = () => Array.from(el.querySelectorAll<HTMLTextAreaElement>('textarea'));
    const handles = () => Array.from(el.querySelectorAll<HTMLButtonElement>('button[aria-label^="Move item"]'));
    const button = (label: RegExp) => Array.from(el.querySelectorAll('button')).find(b => label.test(b.textContent?.trim() || b.getAttribute('aria-label') || '')) as HTMLButtonElement;
    const type = (input: HTMLInputElement | HTMLTextAreaElement, value: string) => {
      input.value = value;
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    };
    const submit = async () => {
      el.querySelector('form')!.dispatchEvent(new Event('submit'));
      await settle();
    };
    return { fixture, el, areas, handles, button, type, submit, settle, done };
  }

  it('opens a file as one box per item, with its description and aliases', async () => {
    const { el, areas } = await render();
    expect(api.memoryFile).toHaveBeenCalledWith('prj_1', 'project', 'sis');
    expect(areas().map(a => a.value)).toEqual(FILE.items.map(i => i.text));
    expect(el.querySelector<HTMLInputElement>('#memory-editor-description')!.value).toBe('Banner conventions');
    expect(el.querySelector<HTMLInputElement>('#memory-editor-aliases')!.value).toBe('banner');
    expect(el.textContent).toContain('Save as version 4');
  });

  it('saves items with the anchors they were read with, a new one without', async () => {
    const { areas, button, type, submit, done } = await render();
    type(areas()[2], 'Section IDs are CRN plus term, as in 41234.202670.');
    button(/Add item/).click();
    await new Promise(r => setTimeout(r, 0));
    type(areas()[3], 'A new fact.');
    await submit();
    expect(api.saveMemoryFile).toHaveBeenCalledWith('prj_1', 'project', 'sis', {
      items: [
        { text: 'Term codes are YYYYTT.', anchor: 'aaaaaaaa' },
        { text: 'Old link to [[gone]].', anchor: 'bbbbbbbb' },
        { text: 'Section IDs are CRN plus term, as in 41234.202670.', anchor: 'cccccccc' },
        { text: 'A new fact.', anchor: null },
      ],
      description: 'Banner conventions',
      aliases: ['banner'],
      baseVersion: 3,
    });
    expect(done).toHaveBeenCalledWith({ slug: 'sis', proposed: false });
  });

  it('keeps a pinned item and announces a removal', async () => {
    const { el, button, areas } = await render();
    expect(button(/Remove item 1/).disabled).toBe(true);
    expect(areas()[0].getAttribute('aria-describedby')).toContain('memory-item-pinned-');
    button(/Remove item 2/).click();
    await new Promise(r => setTimeout(r, 0));
    expect(areas().length).toBe(2);
    expect(el.textContent).toContain('Item 2 removed. It goes to the archive when you save.');
  });

  it('reorders by keyboard: the handles are one tab stop, Alt and an arrow move the item', async () => {
    const { el, handles, areas, fixture } = await render();
    expect(handles().map(h => h.getAttribute('tabindex'))).toEqual(['0', '-1', '-1']);
    handles()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
    fixture.detectChanges();
    expect(handles().map(h => h.getAttribute('tabindex'))).toEqual(['-1', '0', '-1']);
    handles()[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true }));
    fixture.detectChanges();
    expect(areas().map(a => a.value)[2]).toBe('Old link to [[gone]].');
    expect(handles().map(h => h.getAttribute('tabindex'))).toEqual(['-1', '-1', '0']);
    expect(el.textContent).toContain('Item moved to position 3 of 3.');
  });

  it('flags a new dead link live, but not one the item already had', async () => {
    const { el, areas, type } = await render();
    expect(el.textContent).not.toContain('doesn’t match');
    type(areas()[2], 'See [[nowhere]] and [[Throttling]].');
    expect(areas()[2].getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('[aria-live=polite]')?.textContent).toContain('[[nowhere]] doesn’t match a file name or alias.');
    expect(el.textContent).not.toContain('[[Throttling]] doesn’t');
  });

  it('inserts a picked link at the caret', async () => {
    const { areas, button, settle } = await render();
    const area = areas()[2];
    area.setSelectionRange(0, 0);
    button(/Insert a link in item 3/).click();
    await settle();
    expect(dialog.open).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ ariaLabel: 'Link to a file', autoFocus: '#memory-link-filter' }));
    expect(areas()[2].value).toBe('[[rates]]Section IDs are CRN plus term.');
  });

  it('a new file needs a free, well-formed name before it saves', async () => {
    const { el, areas, type, submit } = await render('new');
    expect(el.textContent).toContain('New file');
    type(areas()[0], 'A fact.');
    await submit();
    expect(api.saveMemoryFile).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Give the file a name.');
    const name = el.querySelector<HTMLInputElement>('#memory-editor-name')!;
    type(name, 'Banner');
    expect(el.textContent).toContain('Use lowercase letters');
    type(name, 'banner');
    expect(el.textContent).toContain('A file already has that name or alias.');
    type(name, 'deadlines');
    await submit();
    expect(api.saveMemoryFile).toHaveBeenCalledWith('prj_1', 'project', 'deadlines', expect.objectContaining({ baseVersion: 0, items: [{ text: 'A fact.', anchor: null }] }));
  });

  it('a viewer’s change goes to review as file text', async () => {
    const { areas, type, submit, done, el } = await render('propose');
    expect(el.textContent).toContain('This goes to the project’s editors for review.');
    type(areas()[2], 'Changed.');
    await submit();
    expect(api.saveMemoryFile).not.toHaveBeenCalled();
    expect(api.proposeMemoryChange).toHaveBeenCalledWith('prj_1', {
      slug: 'sis',
      text: '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->\n- Old link to [[gone]]. <!-- e:bbbbbbbb -->\n- Changed. <!-- e:cccccccc -->\n',
      description: 'Banner conventions',
      aliases: ['banner'],
    });
    expect(done).toHaveBeenCalledWith({ slug: 'sis', proposed: true });
  });

  it('shows the server’s sentence when a save is refused', async () => {
    api.saveMemoryFile.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Someone changed this file after you opened it.' } })));
    const { el, submit, done } = await render();
    await submit();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Someone changed this file after you opened it.');
    expect(done).not.toHaveBeenCalled();
  });
});
