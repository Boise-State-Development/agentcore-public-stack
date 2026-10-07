import { describe, it, expect, beforeEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { MemoryArchiveComponent } from './memory-archive.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { UserService } from '../../auth/user.service';
import { ArchivedMemoryItem } from '../models/project.model';
import { memoryEntry } from '../../../testing/project-memory.fixtures';

const ROW = (archiveId: string, slug: string, extra: Partial<ArchivedMemoryItem> = {}): ArchivedMemoryItem => ({
  archiveId,
  slug,
  anchor: 'aaaaaaaa',
  text: `A fact from ${slug}.`,
  reason: 'removed',
  archivedBy: 'dana@x.edu',
  archivedAt: '2026-10-01T12:00:00Z',
  restorableUntil: '2027-10-01T12:00:00Z',
  ...extra,
});

describe('MemoryArchiveComponent', () => {
  const api = { memoryArchive: vi.fn(), restoreMemoryItem: vi.fn() };
  const toast = { success: vi.fn(), error: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.memoryArchive.mockReturnValue(
      of({
        items: [ROW('a1', 'sis'), ROW('a2', 'rates', { reason: 'deleted', archivedBy: 'me@x.edu' })],
        people: { 'dana@x.edu': 'Dana Whitfield' },
      }),
    );
    api.restoreMemoryItem.mockReturnValue(of({ slug: 'sis', version: 4 }));
    TestBed.configureTestingModule({
      imports: [MemoryArchiveComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        { provide: UserService, useValue: { currentUser: signal({ email: 'me@x.edu' }) } },
        provideRouter([]),
      ],
    });
  });

  async function render(canRestore = true) {
    const fixture = TestBed.createComponent(MemoryArchiveComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
    fixture.componentRef.setInput('scope', 'project');
    fixture.componentRef.setInput('entries', [memoryEntry('sis')]);
    fixture.componentRef.setInput('canRestore', canRestore);
    const restored = vi.fn();
    fixture.componentInstance.restored.subscribe(restored);
    fixture.detectChanges();
    for (let i = 0; i < 4; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    const rows = () => Array.from(el.querySelectorAll('ul[aria-label="Archived items"] > li'));
    return { fixture, el, rows, restored };
  }

  it('lists what left each file, who removed it and until when it can come back', async () => {
    const { el, rows } = await render();
    expect(api.memoryArchive).toHaveBeenCalledWith('prj_1', 'project');
    expect(el.textContent).toContain('restorable for a year');
    const [first, second] = rows().map(r => r.textContent?.replace(/\s+/g, ' ') ?? '');
    expect(first).toContain('sis · Removed by Dana Whitfield · Oct 1, 2026 · restorable until Oct 1, 2027');
    expect(second).toContain('Its file was deleted by you');
  });

  it('filters by file', async () => {
    const { el, fixture, rows } = await render();
    const select = el.querySelector<HTMLSelectElement>('#archive-file')!;
    select.value = 'rates';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(rows().length).toBe(1);
    expect(rows()[0].textContent).toContain('A fact from rates.');
  });

  it('restores an item and tells the page', async () => {
    const { fixture, rows, restored } = await render();
    rows()[0].querySelector('button')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.restoreMemoryItem).toHaveBeenCalledWith('prj_1', 'project', 'a1');
    expect(restored).toHaveBeenCalledWith({ slug: 'sis', version: 4 });
    expect(rows().length).toBe(1);
  });

  it('offers no restore without the right to edit', async () => {
    const { rows } = await render(false);
    expect(rows()[0].querySelector('button')).toBeNull();
  });
});
