import { describe, it, expect, beforeEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { MemoryFileViewComponent } from './memory-file-view.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { UserService } from '../../auth/user.service';
import { MemoryFile } from '../models/project.model';
import { MEMORY_LIMITS, memoryEntry } from '../../../testing/project-memory.fixtures';

const FILE: MemoryFile = {
  slug: 'sis',
  description: 'Term codes',
  version: 3,
  tokens: 120,
  items: [
    {
      anchor: 'aaaaaaaa',
      text: 'Batch calls in groups of 50. See [[rates]].',
      pinned: true,
      provenance: { addedBy: 'dana@x.edu', addedAt: '2026-09-18T12:00:00Z', sourceSessionId: 'sess-dana' },
    },
    {
      anchor: 'bbbbbbbb',
      text: 'Run at 02:30.',
      pinned: false,
      provenance: { addedBy: 'me@x.edu', addedAt: '2026-09-20T12:00:00Z', sourceSessionId: 'sess-mine' },
    },
    { anchor: 'cccccccc', text: 'Old fact, see [[gone]].', pinned: false, provenance: null },
  ],
  people: { 'dana@x.edu': 'Dana Whitfield' },
};

describe('MemoryFileViewComponent', () => {
  const api = { memoryFile: vi.fn(), pinMemoryItem: vi.fn(), unpinMemoryItem: vi.fn(), restoreMemoryItem: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.memoryFile.mockReturnValue(of(FILE));
    api.pinMemoryItem.mockReturnValue(of({ slug: 'sis', pinned: ['aaaaaaaa', 'bbbbbbbb'] }));
    api.unpinMemoryItem.mockReturnValue(of({ slug: 'sis', pinned: [] }));
    TestBed.configureTestingModule({
      imports: [MemoryFileViewComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        { provide: UserService, useValue: { currentUser: signal({ email: 'me@x.edu' }) } },
        provideRouter([]),
      ],
    });
  });

  async function render(canEdit = true, tokens = 120) {
    const fixture = TestBed.createComponent(MemoryFileViewComponent);
    const ref = fixture.componentRef;
    ref.setInput('projectId', 'prj_1');
    ref.setInput('scope', 'project');
    ref.setInput('entry', memoryEntry('sis', { description: 'Term codes', aliases: ['term codes'], version: 3, tokens, updatedBy: 'dana@x.edu', updatedByName: 'Dana Whitfield' }));
    ref.setInput('entries', [memoryEntry('sis'), memoryEntry('rates')]);
    ref.setInput('limits', MEMORY_LIMITS);
    ref.setInput('canEdit', canEdit);
    fixture.detectChanges();
    for (let i = 0; i < 4; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    const items = () => Array.from(el.querySelectorAll('ul[aria-label="Items"] > li'));
    return { fixture, el, items };
  }

  it('shows each item with where it came from written under it', async () => {
    const { el, items } = await render();
    expect(api.memoryFile).toHaveBeenCalledWith('prj_1', 'project', 'sis');
    expect(el.textContent).toContain('Version 3');
    expect(el.textContent).toContain('also “term codes”');
    expect(el.textContent).toContain('2 contributors');
    const [first, second, third] = items().map(li => li.textContent?.replace(/\s+/g, ' ') ?? '');
    expect(first).toContain('Pinned · Saved by Dana Whitfield from a task · Sep 18, 2026');
    expect(second).toContain('Saved by you from a task (open it)');
    expect(third).toContain('Added before item history was kept');
  });

  it('links only the caller’s own task, and draws links as chips', async () => {
    const { el, items } = await render();
    const taskLinks = Array.from(el.querySelectorAll<HTMLAnchorElement>('a[href^="/s/"]')).map(a => a.getAttribute('href'));
    expect(taskLinks).toEqual(['/s/sess-mine']);
    expect(items()[0].querySelector('a')?.getAttribute('href')).toContain('file=rates');
    expect(items()[2].textContent).toContain('(no file by that name)');
  });

  it('pins and unpins, and says which state it is in', async () => {
    const { fixture, items } = await render();
    const pin = (li: Element) => li.querySelector('button') as HTMLButtonElement;
    expect(pin(items()[0]).getAttribute('aria-pressed')).toBe('true');
    expect(pin(items()[1]).getAttribute('aria-label')).toBe('Pin: Run at 02:30.');

    pin(items()[1]).click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.pinMemoryItem).toHaveBeenCalledWith('prj_1', 'project', 'sis', 'bbbbbbbb');
    expect(pin(items()[1]).getAttribute('aria-pressed')).toBe('true');

    pin(items()[0]).click();
    await fixture.whenStable();
    expect(api.unpinMemoryItem).toHaveBeenCalledWith('prj_1', 'project', 'sis', 'aaaaaaaa');
  });

  it('offers no pins to a viewer', async () => {
    const { items } = await render(false);
    expect(items()[0].querySelector('button')).toBeNull();
  });

  describe('supersede markers and moved items (2.6c)', () => {
    const MARKED: MemoryFile = {
      ...FILE,
      items: [
        {
          anchor: 'aaaaaaaa',
          text: 'Owner: Priya.',
          pinned: false,
          provenance: { addedBy: 'me@x.edu', addedAt: '2026-09-20T12:00:00Z' },
          replaces: [
            { archiveId: 'arch-1', text: 'Owner: Marcus.', reason: 'superseded', archivedAt: '2026-10-08T12:00:00Z', restorableUntil: '2027-10-08T12:00:00Z' },
          ],
        },
        {
          anchor: 'bbbbbbbb',
          text: 'Batch calls in groups of 50.',
          pinned: false,
          provenance: { addedBy: 'dana@x.edu', addedAt: '2026-09-18T12:00:00Z', movedFrom: 'rates', movedBy: 'me@x.edu', movedAt: '2026-10-08T12:00:00Z' },
          replaces: [
            { archiveId: 'arch-2', text: 'Calls go in batches of 50.', reason: 'merged', archivedAt: '2026-10-08T12:00:00Z', restorableUntil: '2027-10-08T12:00:00Z' },
          ],
        },
        { anchor: 'cccccccc', text: 'Plain.', pinned: false, provenance: null, replaces: [] },
      ],
    };

    beforeEach(() => {
      api.memoryFile.mockReturnValue(of(MARKED));
      api.restoreMemoryItem.mockReturnValue(of({ slug: 'sis', version: 4 }));
    });

    it('marks an item that replaced others, and opens to what it replaced', async () => {
      const { items } = await render();
      const [first, second, third] = items();
      expect(first.querySelector('summary')?.textContent?.trim()).toBe('Replaces an older item');
      expect(second.querySelector('summary')?.textContent?.trim()).toBe('Merged with 1 other item');
      expect(third.querySelector('details')).toBeNull();
      const text = first.querySelector('details')?.textContent?.replace(/\s+/g, ' ') ?? '';
      expect(text).toContain('Replaced: Owner: Marcus.');
      expect(text).toContain('Replaced Oct 8, 2026 · in the archive until Oct 8, 2027');
    });

    it('says which file a moved item came from, as a link', async () => {
      const { items } = await render();
      const line = items()[1].querySelector('p + p') as HTMLElement;
      expect(line.textContent?.replace(/\s+/g, ' ')).toContain('moved here from rates in a tidy-up');
      expect(line.querySelector('a')?.getAttribute('href')).toContain('file=rates');
    });

    it('puts a replaced item back for an editor, and tells the page', async () => {
      const { fixture, items } = await render();
      const restored = vi.fn();
      fixture.componentInstance.restored.subscribe(restored);
      const putBack = items()[0].querySelector('details button') as HTMLButtonElement;
      expect(putBack.getAttribute('aria-label')).toBe('Put it back: Owner: Marcus.');
      putBack.click();
      await fixture.whenStable();
      expect(api.restoreMemoryItem).toHaveBeenCalledWith('prj_1', 'project', 'arch-1');
      expect(restored).toHaveBeenCalledWith({ slug: 'sis', version: 4 });
    });

    it('offers a viewer no Put it back', async () => {
      const { items } = await render(false);
      expect(items()[0].querySelector('details button')).toBeNull();
    });
  });

  it('says under an item, and under the description, what the content check found (2.7)', async () => {
    api.memoryFile.mockReturnValue(
      of({
        ...FILE,
        lint: [
          {
            rule: 'you_must_now', category: 'instruction', where: 'description', excerpt: 'You must now',
            message: 'The description reads like an instruction to the assistant: “You must now”.',
            summary: 'Reads like an instruction to the assistant: “You must now”.',
          },
        ],
        items: [
          FILE.items[0],
          {
            ...FILE.items[1],
            lint: [
              {
                rule: 'aws_access_key', category: 'secret', where: 'item', position: 2, anchor: 'bbbbbbbb',
                label: 'an AWS access key', message: 'Item 2 looks like it contains a credential (an AWS access key).',
                summary: 'Looks like it contains a credential (an AWS access key).',
              },
            ],
          },
          FILE.items[2],
        ],
      }),
    );
    const { el, items } = await render();
    expect(items()[0].textContent).not.toContain('Content check');
    expect(items()[1].textContent).toContain('Content check: Looks like it contains a credential (an AWS access key).');
    expect(el.querySelector('header')?.textContent).toContain('Content check: Reads like an instruction to the assistant');
  });

  it('warns when the file is close to its size limit', async () => {
    const { el } = await render(true, 6500);
    expect(el.textContent).toContain('close to the 8,000-token limit');
    expect(el.querySelector('[role=meter]')?.getAttribute('aria-valuetext')).toBe('6,500 of 8,000 tokens, close to the limit');
  });
});
