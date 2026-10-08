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
  const api = { startMaintenance: vi.fn(), maintenanceRuns: vi.fn(), maintenanceRun: vi.fn() };
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

  async function render(canRun = true) {
    const fixture = TestBed.createComponent(MemoryMaintenanceComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
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
    expect(api.startMaintenance).toHaveBeenCalledWith('prj_1', undefined);
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
    expect(api.startMaintenance).toHaveBeenCalledWith('prj_1', 'canvas');
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
});
