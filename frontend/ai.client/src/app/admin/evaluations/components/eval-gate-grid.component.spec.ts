import { afterEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { EvalGateGridComponent } from './eval-gate-grid.component';
import { runColumns } from '../evaluations.util';
import { evalRun, family } from '../../../../testing/evaluations.fixtures';

const RUNS = [
  evalRun('2026-11-01T09:05:00Z', { families: { tool_selection: family(9), document_qa: family(24, 'pass', 24, 24) } }),
  evalRun('2026-11-02T09:05:00Z', {
    gateFailed: true,
    families: { tool_selection: family(5, 'fail'), document_qa: family(24, 'pass', 24, 24), general_chat: family(7, 'advisory') },
  }),
];

function render(selectedRunId: string | null = null) {
  TestBed.configureTestingModule({});
  const fixture = TestBed.createComponent(EvalGateGridComponent);
  fixture.componentRef.setInput('columns', runColumns(RUNS));
  fixture.componentRef.setInput('selectedRunId', selectedRunId);
  fixture.detectChanges();
  return fixture;
}

const states = (row: Element): (string | null)[] =>
  [...row.querySelectorAll('button')].map(b => b.getAttribute('data-state'));

afterEach(() => TestBed.resetTestingModule());

describe('EvalGateGridComponent', () => {
  it('draws one row per family and one cell per run, oldest first', () => {
    const el: HTMLElement = render().nativeElement;
    const rows = [...el.querySelectorAll('tbody tr')];
    expect(rows.map(r => r.querySelector('th')?.textContent?.trim())).toEqual(['document_qa', 'general_chat', 'tool_selection']);
    expect(states(rows[0])).toEqual(['pass', 'pass']);
    expect(states(rows[1])).toEqual(['not_run', 'advisory']);
    expect(states(rows[2])).toEqual(['pass', 'fail']);
    const headers = [...el.querySelectorAll('thead th[scope="col"]')].slice(1).map(h => h.textContent?.replace(/\s+/g, ' ').trim());
    expect(headers[0]).toContain('2026-11-01 09:05 UTC');
  });

  it("names each cell with its run and its §8 row, so hover and a screen reader say the same", () => {
    const el: HTMLElement = render().nativeElement;
    const failed = el.querySelector('tbody tr:nth-child(3) button[data-state="fail"]') as HTMLButtonElement;
    expect(failed.getAttribute('aria-label')).toBe(
      '2026-11-02 09:05 UTC · tool_selection: n 9, base 9, run 5, flips +0/-4, McNemar p 1.000, gate fail',
    );
    expect(failed.getAttribute('title')).toBe(failed.getAttribute('aria-label'));
    const notRun = el.querySelector('button[data-state="not_run"]') as HTMLButtonElement;
    expect(notRun.getAttribute('aria-label')).toBe('2026-11-01 09:05 UTC · general_chat: not run');
  });

  it('tells a state by a glyph as well as a colour', () => {
    const el: HTMLElement = render().nativeElement;
    const glyph = (state: string) => el.querySelector(`button[data-state="${state}"]`)?.textContent?.trim();
    expect([glyph('pass'), glyph('fail'), glyph('advisory'), glyph('not_run')]).toEqual(['✓', '✕', '~', '·']);
  });

  it('marks the chosen run and emits a run when a cell is chosen', () => {
    const fixture = render('2026-11-02T09:05:00Z');
    const el: HTMLElement = fixture.nativeElement;
    const pressed = [...el.querySelectorAll('button[aria-pressed="true"]')];
    expect(pressed).toHaveLength(3);
    const emitted: string[] = [];
    fixture.componentInstance.runSelect.subscribe(id => emitted.push(id));
    (el.querySelector('tbody tr button') as HTMLButtonElement).click();
    expect(emitted).toEqual(['2026-11-01T09:05:00Z']);
  });

  it('says so when there are no runs', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(EvalGateGridComponent);
    fixture.componentRef.setInput('columns', []);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No runs for this model and track in the range.');
  });
});
