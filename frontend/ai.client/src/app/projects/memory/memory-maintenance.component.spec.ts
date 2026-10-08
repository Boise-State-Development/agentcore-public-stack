import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { MAINTENANCE_POLL_MS, MemoryMaintenanceComponent } from './memory-maintenance.component';
import { ProjectApiService } from '../services/project-api.service';
import { MaintenanceRun } from '../models/project.model';

function run(overrides: Partial<MaintenanceRun> = {}): MaintenanceRun {
  return {
    runId: 'r1',
    state: 'queued',
    slug: null,
    requestedByEmail: 'ed@x.edu',
    createdAt: '2026-10-07T12:00:00Z',
    results: [],
    ...overrides,
  };
}

describe('MemoryMaintenanceComponent', () => {
  const api = { startMaintenance: vi.fn(), maintenanceRuns: vi.fn(), maintenanceRun: vi.fn(), undoMaintenance: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    vi.useFakeTimers();
    api.maintenanceRuns.mockReturnValue(of({ runs: [] }));
    api.startMaintenance.mockReturnValue(of(run()));
    dialog.open.mockReturnValue({ closed: of(true) });
    TestBed.configureTestingModule({
      imports: [MemoryMaintenanceComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: Dialog, useValue: dialog },
        provideRouter([]),
      ],
    });
  });

  afterEach(() => vi.useRealTimers());

  async function render(canRun = true, scope: 'project' | 'mine' = 'project') {
    const fixture = TestBed.createComponent(MemoryMaintenanceComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
    fixture.componentRef.setInput('scope', scope);
    fixture.componentRef.setInput('canRun', canRun);
    const finished = vi.fn();
    fixture.componentInstance.finished.subscribe(finished);
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const tick = async (ms: number) => {
      await vi.advanceTimersByTimeAsync(ms);
      fixture.detectChanges();
    };
    return { fixture, el, finished, tick, button: () => el.querySelector('button') as HTMLButtonElement };
  }

  it('starts after the confirmation and follows the run until it ends', async () => {
    api.maintenanceRun
      .mockReturnValueOnce(of(run({ state: 'running' })))
      .mockReturnValueOnce(of(run({
        state: 'done',
        results: [
          { slug: 'canvas', outcome: 'proposed', proposalId: 'c1', planned: 2, kept: 2, dropped: 0 },
          { slug: 'sis', outcome: 'nothing_to_do', planned: 0, kept: 0, dropped: 0 },
          { slug: 'old', outcome: 'failed', planned: 0, kept: 0, dropped: 0 },
        ],
      })));
    const { el, finished, tick, button } = await render();

    button().click();
    await tick(0);
    expect(dialog.open.mock.calls[0][1].data.message).toContain('nothing in memory changes until an editor approves');
    expect(api.startMaintenance).toHaveBeenCalledWith('prj_1', undefined, 'project');
    expect(el.textContent).toContain('Tidying up project memory');
    expect(button().disabled).toBe(true);

    await tick(MAINTENANCE_POLL_MS);
    expect(el.textContent).toContain('Tidying up');
    await tick(MAINTENANCE_POLL_MS);
    expect(el.textContent).toContain('Maintenance suggested changes to 1 file.');
    expect(el.textContent).toContain('1 file couldn’t be checked.');
    expect(el.querySelector('a')?.textContent).toContain('Review them');
    expect(finished).toHaveBeenCalledTimes(1);

    await tick(MAINTENANCE_POLL_MS * 3);
    expect(api.maintenanceRun).toHaveBeenCalledTimes(2);
  });

  it('runs on one file', async () => {
    api.startMaintenance.mockReturnValue(of(run({ slug: 'canvas' })));
    const { fixture, el, tick } = await render();
    void fixture.componentInstance.start('canvas');
    await tick(0);
    expect(dialog.open.mock.calls[0][1].data.title).toBe('Tidy up “canvas”?');
    expect(api.startMaintenance).toHaveBeenCalledWith('prj_1', 'canvas', 'project');
    expect(el.textContent).toContain('Tidying up “canvas”');
  });

  it('does nothing when the confirmation is cancelled', async () => {
    dialog.open.mockReturnValue({ closed: of(undefined) });
    const { tick, button } = await render();
    button().click();
    await tick(0);
    expect(api.startMaintenance).not.toHaveBeenCalled();
  });

  it('picks up a run already in flight', async () => {
    api.maintenanceRuns.mockReturnValue(of({ runs: [run({ state: 'running' })] }));
    api.maintenanceRun.mockReturnValue(of(run({ state: 'failed', error: 'Maintenance didn’t finish. Try running it again.' })));
    const { el, tick } = await render();
    expect(el.textContent).toContain('Tidying up project memory');
    await tick(MAINTENANCE_POLL_MS);
    expect(el.textContent).toContain('Maintenance didn’t finish. Try running it again.');
  });

  it('says why it couldn’t start', async () => {
    api.startMaintenance.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Maintenance is already running on this project’s memory.' } })),
    );
    const { el, tick, button } = await render();
    button().click();
    await tick(0);
    expect(el.textContent).toContain('Maintenance is already running');
  });

  it('a viewer gets no button and nothing is read', async () => {
    const { el } = await render(false);
    expect(el.querySelector('button')).toBeNull();
    expect(api.maintenanceRuns).not.toHaveBeenCalled();
  });

  describe('your own memory (2.6b)', () => {
    const merge = {
      type: 'merge' as const,
      sources: [{ anchor: 'a1', text: 'Batch calls in groups of 50.' }, { anchor: 'a2', text: 'Calls go in batches of 50.' }],
      text: 'Batch calls in groups of 50.',
      keep: 'a1',
      why: 'The same rule twice',
    };
    const prune = {
      type: 'prune' as const,
      sources: [{ anchor: 'a3', text: 'Kickoff on 2026-09-14.' }],
      reason: 'expired' as const,
      why: 'It happened',
    };
    const supersede = {
      type: 'supersede' as const,
      sources: [{ anchor: 'a4', text: 'Reviewer is Marcus.' }, { anchor: 'a5', text: 'Reviewer is now Priya.' }],
      why: 'Priya replaced Marcus',
    };
    const applied = (overrides: Partial<MaintenanceRun> = {}) => run({
      scope: 'mine',
      state: 'done',
      finishedAt: new Date().toISOString(),
      undoableUntil: '2026-11-06T12:00:00Z',
      results: [
        { slug: 'canvas', outcome: 'applied', planned: 3, kept: 3, dropped: 0, version: 2, ops: [merge, supersede, prune] },
        { slug: 'sis', outcome: 'changed', planned: 0, kept: 0, dropped: 0 },
      ],
      ...overrides,
    });

    it('says the changes are saved, then shows what changed with Undo', async () => {
      api.startMaintenance.mockReturnValue(of(run({ scope: 'mine' })));
      api.maintenanceRun.mockReturnValue(of(applied()));
      const { el, finished, tick, button } = await render(true, 'mine');
      expect(api.maintenanceRuns).toHaveBeenCalledWith('prj_1', 'mine');

      button().click();
      await tick(0);
      const data = dialog.open.mock.calls[0][1].data;
      expect(data.title).toBe('Tidy up your memory?');
      expect(data.message).toContain('saved straight away, and you can undo them');
      expect(api.startMaintenance).toHaveBeenCalledWith('prj_1', undefined, 'mine');
      expect(el.textContent).toContain('Tidying up your memory');

      await tick(MAINTENANCE_POLL_MS);
      expect(api.maintenanceRun).toHaveBeenCalledWith('prj_1', 'r1', 'mine');
      expect(finished).toHaveBeenCalledTimes(1);
      expect(el.textContent).toContain('Tidied up your memory: 3 changes to 1 file.');
      expect(el.textContent).toContain('“sis” was saved while it ran, so it was left as it is.');
      expect(el.querySelector('a')?.textContent).not.toContain('Review them');
      const details = el.querySelector('details') as HTMLDetailsElement;
      expect(details.textContent).toContain('Merged 2 items that said the same thing into “Batch calls in groups of 50.”');
      expect(details.textContent).toContain('Removed “Kickoff on 2026-09-14.”, whose dates have passed');
      expect(details.textContent).toContain('Removed “Reviewer is Marcus.”, since the newer “Reviewer is now Priya.” updates it');
      expect(el.textContent).toContain('You can undo this until Nov 6.');
    });

    it('undoes, and says which files were left alone', async () => {
      api.maintenanceRuns.mockReturnValue(of({ runs: [applied()] }));
      api.undoMaintenance.mockReturnValue(of(applied({
        undoneAt: '2026-10-08T13:00:00Z',
        undoableUntil: null,
        results: [
          { slug: 'canvas', outcome: 'applied', planned: 2, kept: 2, dropped: 0, ops: [merge, prune], undo: 'restored', undoVersion: 3 },
          { slug: 'notes', outcome: 'applied', planned: 1, kept: 1, dropped: 0, ops: [prune], undo: 'changed' },
        ],
      })));
      const { el, finished, tick } = await render(true, 'mine');
      expect(el.textContent).toContain('Tidied up your memory');

      const undo = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Undo')) as HTMLButtonElement;
      undo.click();
      await tick(0);
      expect(api.undoMaintenance).toHaveBeenCalledWith('prj_1', 'r1');
      expect(finished).toHaveBeenCalledTimes(1);
      expect(el.textContent).toContain('Undone: 1 file is back as before. 1 file changed since was left as it is.');
      expect(el.textContent).toContain('Saved after the tidy-up, so it was left as it is.');
      expect([...el.querySelectorAll('button')].some(b => b.textContent?.includes('Undo'))).toBe(false);
    });

    it('shows an older or dismissed tidy-up only while it is recent', async () => {
      api.maintenanceRuns.mockReturnValue(of({ runs: [applied({ finishedAt: '2026-01-01T00:00:00Z' })] }));
      const { el } = await render(true, 'mine');
      expect(el.textContent).not.toContain('Tidied up');
    });

    it('says why an undo was refused', async () => {
      api.maintenanceRuns.mockReturnValue(of({ runs: [applied()] }));
      api.undoMaintenance.mockReturnValue(
        throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'This tidy-up was already undone.' } })),
      );
      const { el, tick } = await render(true, 'mine');
      ([...el.querySelectorAll('button')].find(b => b.textContent?.includes('Undo')) as HTMLButtonElement).click();
      await tick(0);
      expect(el.textContent).toContain('This tidy-up was already undone.');
    });
  });
});
