import { afterEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { EvaluationsPage } from './evaluations.page';
import { ReleaseLedgerHttpService } from '../costs/services/release-ledger-http.service';
import { LedgerEvalRun } from '../costs/models';
import { HAIKU_45, HAIKU_55, evalRun, evalsResponse, family } from '../../../testing/evaluations.fixtures';

// Chart.js needs a 2D context jsdom does not have; the charts' own rendering is not under test here.
vi.mock('chart.js/auto', () => ({ Chart: class { destroy(): void {} } }));

const RUNS: LedgerEvalRun[] = [
  evalRun('2026-11-08T09:05:00Z', { gateFailed: true, families: { tool_selection: family(6, 'fail') } }),
  evalRun('2026-11-07T16:00:00Z', { track: 'release', k: 3 }),
  evalRun('2026-11-07T09:05:00Z'),
  evalRun('2026-11-06T09:05:00Z', { build: 'aaaaaaaaaaaaaaaa' }),
  evalRun('2026-11-05T12:00:00Z', { modelId: HAIKU_45 }),
];

async function render(query: Record<string, string> = {}) {
  const getEvals = vi.fn(() => of(evalsResponse(RUNS)));
  TestBed.configureTestingModule({
    providers: [
      { provide: ReleaseLedgerHttpService, useValue: { getEvals } },
      { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(query) } } },
    ],
  });
  const fixture = TestBed.createComponent(EvaluationsPage);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement, getEvals };
}

const text = (el: Element | null): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

afterEach(() => TestBed.resetTestingModule());

describe('EvaluationsPage', () => {
  it('opens on the model with the most runs and its nightly track, one model per chart (D22)', async () => {
    const { el, getEvals } = await render();
    expect(getEvals).toHaveBeenCalledTimes(1);
    expect((el.querySelector('#eval-model') as HTMLSelectElement).value).toBe(HAIKU_55);
    const pressed = el.querySelector('fieldset button[aria-pressed="true"]');
    expect(text(pressed)).toBe('Nightly 3');
    // Three nightly runs of Haiku 5.5: the 4.5 run and the release run are not in the grid.
    expect(el.querySelectorAll('[data-testid="gate-grid"] tbody tr button')).toHaveLength(3);
    expect(el.querySelectorAll('[data-testid="run-table"] tbody tr')).toHaveLength(3);
  });

  it('shows the latest run of every track for the chosen model', async () => {
    const { el } = await render();
    const nightly = el.querySelector('[data-track="nightly"]');
    expect(text(nightly)).toContain('gate failed');
    expect(text(nightly)).toContain('2026-11-08 09:05 UTC');
    expect(text(el.querySelector('[data-track="release"]'))).toContain('gate passed');
  });

  it('draws the seven sections in order', async () => {
    const { el } = await render();
    const headings = [...el.querySelectorAll('h2')].map(h => text(h));
    expect(headings).toEqual([
      'Latest run per track',
      'Gate history',
      'Family trends',
      'Cases',
      'Judge trends (advisory, never a gate)',
      'Run cost and time',
      'Run detail',
    ]);
  });

  it('opens the newest run by default and says where answers live', async () => {
    const { el } = await render();
    const detail = el.querySelector('[data-testid="run-detail"]');
    expect(text(detail)).toContain('2026-11-08 09:05 UTC · gate failed');
    expect(text(detail)).toContain('score --explain');
  });

  it('honours a link from the quality panel: model, track, build and run', async () => {
    const { el } = await render({ model: HAIKU_55, track: 'nightly', build: 'aaaaaaaaaaaaaaaa', run: '2026-11-06T09:05:00Z' });
    expect(text(el.querySelector('[data-testid="build-filter"]'))).toContain('aaaaaaaa');
    expect(el.querySelectorAll('[data-testid="run-table"] tbody tr')).toHaveLength(1);
    expect(text(el.querySelector('[data-testid="run-detail"]'))).toContain('2026-11-06 09:05 UTC');
  });

  it('switches track without another read', async () => {
    const { fixture, el, getEvals } = await render();
    const release = [...el.querySelectorAll('fieldset button')].find(b => text(b).startsWith('Release')) as HTMLButtonElement;
    release.click();
    fixture.detectChanges();
    expect(getEvals).toHaveBeenCalledTimes(1);
    expect(el.querySelectorAll('[data-testid="run-table"] tbody tr')).toHaveLength(1);
  });
});
