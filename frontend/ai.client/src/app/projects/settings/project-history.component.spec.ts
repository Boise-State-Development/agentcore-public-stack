import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectHistoryComponent } from './project-history.component';

const VERSIONS = [3, 2, 1].map(version => ({
  version,
  changes: ['instructions'],
  createdByEmail: version > 1 ? 'me@x.edu' : null,
  createdByName: null,
  createdAt: '2026-10-01T00:00:00Z',
}));

describe('ProjectHistoryComponent', () => {
  const api = { versions: vi.fn(), version: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.versions.mockReturnValue(of({ versions: VERSIONS }));
    api.version.mockImplementation((_id: string, version: number) =>
      of({ version, instructionsDiff: ['+Be terse.'], fieldChanges: [] }),
    );
    TestBed.configureTestingModule({ providers: [{ provide: ProjectApiService, useValue: api }] });
  });

  async function render(initialVersion: number | null) {
    const fixture = TestBed.createComponent(ProjectHistoryComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
    fixture.componentRef.setInput('initialVersion', initialVersion);
    for (let i = 0; i < 3; i++) {
      fixture.detectChanges();
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
    }
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const expanded = Array.from(el.querySelectorAll('button[aria-expanded="true"]')).map(b => b.textContent?.replace(/\s+/g, ' ').trim());
    return { el, expanded };
  }

  it('opens nothing by itself', async () => {
    const { expanded } = await render(null);
    expect(expanded).toEqual([]);
    expect(api.version).not.toHaveBeenCalled();
  });

  it('opens the version a link names, with its diff', async () => {
    const { el, expanded } = await render(2);
    expect(expanded).toHaveLength(1);
    expect(expanded[0]).toContain('Version 2');
    expect(api.version).toHaveBeenCalledWith('prj_1', 2);
    expect(el.querySelector('#version-2')?.textContent).toContain('+Be terse.');
  });

  it('ignores a version the history does not have', async () => {
    const { expanded } = await render(9);
    expect(expanded).toEqual([]);
    expect(api.version).not.toHaveBeenCalled();
  });
});
