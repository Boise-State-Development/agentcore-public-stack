import { describe, it, expect, beforeEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { MemoryHistoryComponent } from './memory-history.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { UserService } from '../../auth/user.service';
import { MemoryFileVersion } from '../models/project.model';
import { memoryEntry } from '../../../testing/project-memory.fixtures';

const v = (version: number, reason: string, updatedBy = 'dana@x.edu'): MemoryFileVersion => ({
  version,
  contentHash: `h${version}`,
  size: 10,
  tokens: 10,
  updatedBy,
  updatedByName: updatedBy === 'dana@x.edu' ? 'Dana Whitfield' : null,
  updatedAt: '2026-10-07T12:00:00Z',
  reason,
});

describe('MemoryHistoryComponent', () => {
  const api = { memoryHistory: vi.fn(), memoryFile: vi.fn(), memoryVersion: vi.fn(), restoreMemoryVersion: vi.fn() };
  const toast = { success: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.memoryHistory.mockReturnValue(of({ slug: 'sis', versions: [v(3, 'proposal'), v(2, 'edit', 'me@x.edu'), v(1, 'save')] }));
    api.memoryFile.mockReturnValue(
      of({ slug: 'sis', description: '', version: 3, tokens: 10, people: {}, items: [{ anchor: 'aaaaaaaa', text: 'Kept.', pinned: false }, { anchor: 'cccccccc', text: 'New.', pinned: false }] }),
    );
    api.memoryVersion.mockImplementation((_s: string, _slug: string, n: number) =>
      of({ ...v(n, 'edit'), slug: 'sis', content: '---\nname: sis\n---\n- Kept. <!-- e:aaaaaaaa -->\n- Gone. <!-- e:bbbbbbbb -->\n' }),
    );
    api.restoreMemoryVersion.mockReturnValue(of({ slug: 'sis', version: 4 }));
    TestBed.configureTestingModule({
      imports: [MemoryHistoryComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        { provide: UserService, useValue: { currentUser: signal({ email: 'me@x.edu' }) } },
        provideRouter([]),
      ],
    });
  });

  async function render(canEdit = true) {
    const fixture = TestBed.createComponent(MemoryHistoryComponent);
    const ref = fixture.componentRef;
    ref.setInput('projectId', 'prj_1');
    ref.setInput('scope', 'project');
    ref.setInput('spaceId', 'spc_p');
    ref.setInput('entry', memoryEntry('sis', { version: 3 }));
    ref.setInput('entries', [memoryEntry('sis')]);
    ref.setInput('canEdit', canEdit);
    const restored = vi.fn();
    fixture.componentInstance.restored.subscribe(restored);
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
    const button = (label: RegExp) => Array.from(el.querySelectorAll('button')).find(b => label.test(b.textContent ?? '')) as HTMLButtonElement;
    return { fixture, el, button, settle, restored };
  }

  it('lists versions newest first with why and who, and opens the one before current', async () => {
    const { el } = await render();
    const rows = Array.from(el.querySelectorAll('ol[aria-label="Versions"] button')).map(b => b.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows[0]).toMatch(/v3 Approved proposal\s*Current\s*Dana Whitfield/);
    expect(rows[1]).toMatch(/v2 Edited\s*You/);
    expect(rows[2]).toContain('v1 Saved by the assistant');
    expect(api.memoryVersion).toHaveBeenCalledWith('spc_p', 'sis', 2);
    expect(el.querySelector('ol button[aria-current="true"]')?.textContent).toContain('v2');
  });

  it('compares the version with now and restores it', async () => {
    const { el, button, settle, restored } = await render();
    expect(el.textContent).toContain('Restoring it brings back 1 item and takes out 1 item.');
    expect(el.textContent).toContain('Not in version 2: New.');
    expect(el.textContent).toContain('Not in the file now: Gone.');
    button(/Restore version 2/).click();
    await settle();
    expect(api.restoreMemoryVersion).toHaveBeenCalledWith('prj_1', 'project', 'sis', 2);
    expect(restored).toHaveBeenCalledWith({ slug: 'sis', version: 4 });
  });

  it('shows why a restore was refused', async () => {
    api.restoreMemoryVersion.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 400, error: { detail: 'Keep pinned items, or unpin them first.' } })));
    const { el, button, settle } = await render();
    button(/Restore version 2/).click();
    await settle();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Keep pinned items');
  });

  it('offers no restore without the right to edit, and none for the current version', async () => {
    const { el, button, settle } = await render(false);
    expect(button(/Restore version/)).toBeUndefined();
    button(/v3/).click();
    await settle();
    expect(el.textContent).toContain('This is the current version.');
  });
});
