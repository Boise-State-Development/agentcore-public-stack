import { describe, it, expect, beforeEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectMemoryPage } from './project-memory.page';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { UserService } from '../../auth/user.service';
import { Project } from '../models/project.model';
import { MEMORY_LIMITS, MEMORY_PROJECT as PROJECT, memoryEntry } from '../../../testing/project-memory.fixtures';

describe('ProjectMemoryPage', () => {
  const api = {
    get: vi.fn(),
    memory: vi.fn(),
    memoryEntries: vi.fn(),
    memoryIndex: vi.fn(),
    memoryFile: vi.fn(),
    proposals: vi.fn(),
    proposal: vi.fn(),
    memoryArchive: vi.fn(),
    deleteMemoryFile: vi.fn(),
    createMyMemory: vi.fn(),
  };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.get.mockReturnValue(of(PROJECT));
    api.memory.mockReturnValue(of({ sharedSpaceId: 'spc_p', personalSpaceId: 'spc_m', role: 'editor', limits: MEMORY_LIMITS }));
    api.memoryEntries.mockImplementation((spaceId: string) =>
      of({ entries: spaceId === 'spc_p' ? [memoryEntry('rates', { description: 'Throttling limits' }), memoryEntry('sis')] : [memoryEntry('prefs')] }),
    );
    api.memoryIndex.mockImplementation((spaceId: string) =>
      of({ content: spaceId === 'spc_p' ? '# Project\n- [[rates]] — limits\n- [[nowhere]]' : '' }),
    );
    api.memoryFile.mockImplementation((_p: string, _s: string, slug: string) => of({ slug, description: '', version: 1, tokens: 10, items: [], people: {} }));
    api.proposals.mockReturnValue(of({ proposals: [{ proposalId: 'p1', slug: 'rates', baseVersion: 1, createdAt: '2026-10-07T12:00:00Z', proposedByEmail: 'v@x.edu', isMine: false }] }));
    api.proposal.mockReturnValue(throwError(() => new Error('not needed')));
    api.memoryArchive.mockReturnValue(of({ items: [], people: {} }));
    api.deleteMemoryFile.mockReturnValue(of(undefined));
    api.createMyMemory.mockReturnValue(of({ spaceId: 'spc_new' }));
    dialog.open.mockReturnValue({ closed: of(true) });
    TestBed.configureTestingModule({
      imports: [ProjectMemoryPage],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
        { provide: UserService, useValue: { currentUser: signal({ email: 'me@x.edu' }) } },
        { provide: Dialog, useValue: dialog },
        provideRouter([]),
      ],
    });
  });

  async function render(query: { scope?: string; file?: string; view?: string } = {}) {
    const fixture = TestBed.createComponent(ProjectMemoryPage);
    fixture.componentRef.setInput('id', 'prj_1');
    for (const [key, value] of Object.entries(query)) fixture.componentRef.setInput(key, value);
    fixture.detectChanges();
    for (let i = 0; i < 4; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    const files = () => Array.from(el.querySelectorAll('nav[aria-label="Memory files"] a'));
    const current = () => el.querySelector('nav[aria-label="Memory files"] a[aria-current="page"]')?.textContent?.replace(/\s+/g, ' ').trim();
    return { fixture, el, files, current };
  }

  it('lists the project’s files after the index, counts both scopes and opens the first file', async () => {
    const { el, files, current } = await render();
    expect(api.memoryEntries).toHaveBeenCalledWith('spc_p');
    expect(api.memoryEntries).toHaveBeenCalledWith('spc_m');
    expect(el.querySelector('nav[aria-label="Memory scope"]')?.textContent?.replace(/\s+/g, ' ')).toContain('Project · 2 Just me · 1');
    expect(files().map(a => a.textContent?.includes('Index') ? 'index' : a.querySelector('.font-mono')?.textContent?.trim())).toEqual(['index', 'rates', 'sis']);
    expect(current()).toContain('rates');
    expect(api.memoryFile).toHaveBeenCalledWith('prj_1', 'project', 'rates');
  });

  it('shows the index with its links and its budget', async () => {
    const { el } = await render({ file: 'MEMORY.md' });
    const article = el.querySelector('article')!;
    expect(article.textContent).toContain('Loads into every member’s tasks');
    expect(article.querySelector('a')?.getAttribute('href')).toContain('file=rates');
    expect(article.textContent).toContain('(no file by that name)');
    expect(article.querySelector('[role=meter]')?.getAttribute('aria-valuemax')).toBe('2000');
  });

  it('offers review with the waiting count, and the archive', async () => {
    const { el } = await render();
    const links = Array.from(el.querySelectorAll('a')).map(a => a.textContent?.replace(/\s+/g, ' ').trim());
    expect(links).toContain('Review 1 waiting');
    expect(links).toContain('Archive');
  });

  it('opens the review queue in place of the files', async () => {
    const { el } = await render({ view: 'review' });
    expect(el.textContent).toContain('Review queue');
    expect(el.querySelector('nav[aria-label="Memory files"]')).toBeNull();
  });

  it('“Just me” reads the caller’s own space and its smaller index budget, with no review', async () => {
    const { el, current } = await render({ scope: 'mine', view: 'review' });
    expect(current()).toContain('prefs');
    expect(el.textContent).not.toContain('Review queue');
    expect(el.textContent).toContain('Only you can see these.');
    const index = el.querySelector('nav[aria-label="Memory files"] [role=meter]');
    expect(index?.getAttribute('aria-valuemax')).toBe('1000');
  });

  it('explains an empty “Just me” before the member keeps anything', async () => {
    api.memory.mockReturnValue(of({ sharedSpaceId: 'spc_p', personalSpaceId: null, role: 'viewer', limits: MEMORY_LIMITS }));
    api.get.mockReturnValue(of({ ...PROJECT, role: 'viewer' } satisfies Project));
    const { el } = await render({ scope: 'mine' });
    expect(el.textContent).toContain('Nothing of your own here yet');
    expect(el.textContent).toContain('Just me · 0');
  });

  it('says when the project has no memory to show', async () => {
    api.memory.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'no memory' } })));
    const { el } = await render();
    expect(el.textContent).toContain('Memory isn’t available for this project.');
    expect(el.querySelector('nav[aria-label="Memory scope"]')).toBeNull();
  });

  it('says when the project can’t be opened', async () => {
    api.get.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    const { el } = await render();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('doesn’t exist, or you’re not a member');
  });

  it('opens the editor in place of the files for Edit and New file', async () => {
    const { el, fixture } = await render();
    const click = async (label: RegExp) => {
      (Array.from(el.querySelectorAll('button')).find(b => label.test(b.textContent ?? '')) as HTMLButtonElement).click();
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
    };
    await click(/^\s*Edit\s*$/);
    expect(el.querySelector('app-memory-editor')).not.toBeNull();
    expect(el.querySelector('nav[aria-label="Memory files"]')).toBeNull();
    await click(/^\s*Cancel\s*$/);
    await click(/New file/);
    expect(el.querySelector('app-memory-editor h2')?.textContent).toContain('New file');
  });

  it('offers a viewer Propose instead of Edit, and no delete', async () => {
    api.get.mockReturnValue(of({ ...PROJECT, role: 'viewer' } satisfies Project));
    const { el } = await render();
    const labels = Array.from(el.querySelectorAll('button')).map(b => b.textContent?.trim());
    expect(labels).toContain('Propose a file');
    expect(labels).toContain('Propose a change');
    expect(labels).not.toContain('Edit');
    expect(el.querySelector('button[aria-label^="Delete"]')).toBeNull();
  });

  it('deletes a file after confirming', async () => {
    const { el, fixture } = await render();
    (el.querySelector('button[aria-label="Delete rates"]') as HTMLButtonElement).click();
    for (let i = 0; i < 3; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
    }
    expect(dialog.open).toHaveBeenCalled();
    expect(api.deleteMemoryFile).toHaveBeenCalledWith('prj_1', 'project', 'rates');
  });

  it('starts “Just me” with a first file', async () => {
    api.memory.mockReturnValue(of({ sharedSpaceId: 'spc_p', personalSpaceId: null, role: 'editor', limits: MEMORY_LIMITS }));
    const { el, fixture } = await render({ scope: 'mine' });
    (Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('New file')) as HTMLButtonElement).click();
    for (let i = 0; i < 4; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
      fixture.detectChanges();
    }
    expect(api.createMyMemory).toHaveBeenCalledWith('prj_1');
    expect(api.memoryEntries).toHaveBeenCalledWith('spc_new');
    expect(el.querySelector('app-memory-editor h2')?.textContent).toContain('New file');
  });
});
