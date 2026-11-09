import { describe, expect, it } from 'vitest';
import {
  countFlips,
  failedCases,
  familyTrends,
  flipMatrix,
  gateGrid,
  isNoisy,
  latestByTrack,
  runColumns,
} from './evaluations.util';
import { evalCase, evalRun, family } from '../../../testing/evaluations.fixtures';

const BUILD = 'aaaa';
const OTHER = 'bbbb';

function outcomes(build: string, passes: boolean[]) {
  return passes.map(pass => ({ build, pass }));
}

describe('the noisy-case rule (§16.3 item 4)', () => {
  it('counts flips as changes between consecutive outcomes', () => {
    expect(countFlips([true, true, true])).toBe(0);
    expect(countFlips([true, false, true, false])).toBe(3);
    expect(countFlips([false, false, true, true])).toBe(1);
  });

  it('tags a case that flips three times in ten runs on one build', () => {
    expect(isNoisy(outcomes(BUILD, [true, false, true, false]))).toBe(true);
    expect(isNoisy(outcomes(BUILD, [true, true, false, true, true, true, false, true, true, true]))).toBe(true);
  });

  it('does not tag two flips, however many runs', () => {
    expect(isNoisy(outcomes(BUILD, [true, false, true]))).toBe(false);
    expect(isNoisy(outcomes(BUILD, [true, true, true, false, false, false, true, true, true, true]))).toBe(false);
  });

  it('does not tag a regression: a steady fail after a steady pass is one flip', () => {
    expect(isNoisy(outcomes(BUILD, [true, true, true, true, true, false, false, false, false, false]))).toBe(false);
  });

  it('never counts a change of build as a flip', () => {
    // Alternating builds, each one steady: a fix and a regression, not noise.
    const mixed = [
      { build: BUILD, pass: true },
      { build: OTHER, pass: false },
      { build: BUILD, pass: true },
      { build: OTHER, pass: false },
      { build: BUILD, pass: true },
      { build: OTHER, pass: false },
    ];
    expect(isNoisy(mixed)).toBe(false);
  });

  it('reads the flips within ten consecutive runs, not across a longer history', () => {
    // Three flips, but spread over twelve runs: no window of ten holds all three.
    const spread = [true, false, true, true, true, true, true, true, true, true, true, false];
    expect(countFlips(spread)).toBe(3);
    expect(isNoisy(outcomes(BUILD, spread))).toBe(false);
    // The same three flips inside the last ten runs are noisy.
    const recent = [true, true, true, true, true, true, true, true, false, true, false, true];
    expect(isNoisy(outcomes(BUILD, recent))).toBe(true);
  });
});

describe('flipMatrix', () => {
  const pattern = [true, false, true, false];
  const runs = pattern.map((pass, i) =>
    evalRun(`2026-11-0${i + 1}T09:05:00Z`, {
      cases: {
        'tool_selection.flaky': evalCase(pass),
        'tool_selection.steady': evalCase(true),
        'tool_selection.broken': evalCase(false, { codes: ['tool_missing'], names: ['list_events'] }),
        'document_qa.other_family': evalCase(true),
      },
    }),
  );
  const rows = flipMatrix(runColumns(runs), 'tool_selection');

  it('has one row per case of the family, without its prefix, and one cell per run', () => {
    expect(rows.map(r => r.label)).toEqual(['broken', 'flaky', 'steady']);
    expect(rows.every(r => r.cells.length === 4)).toBe(true);
  });

  it('tags the flaky case noisy and no other', () => {
    expect(rows.filter(r => r.noisy).map(r => r.label)).toEqual(['flaky']);
  });

  it('marks a failure against a passing baseline, with its codes and names on hover', () => {
    const broken = rows.find(r => r.label === 'broken')!;
    expect(broken.cells.every(c => c.state === 'fail' && c.regressed)).toBe(true);
    expect(broken.cells[0].title).toContain('codes tool_missing');
    expect(broken.cells[0].title).toContain('about list_events');
  });

  it('does not mark a failure the baseline shares, and shows a missing case as absent', () => {
    const cols = runColumns([
      evalRun('2026-11-01T09:05:00Z', { cases: { 'tool_selection.a': evalCase(false, { base: false }) } }),
      evalRun('2026-11-02T09:05:00Z', { cases: {} }),
    ]);
    const [row] = flipMatrix(cols, 'tool_selection');
    expect(row.cells.map(c => [c.state, c.regressed])).toEqual([['fail', false], ['absent', false]]);
  });
});

describe('gateGrid', () => {
  it('has a cell per family and run, oldest first, with not-run where a family is missing', () => {
    const runs = [
      evalRun('2026-11-03T09:05:00Z', {
        gateFailed: true,
        families: { tool_selection: family(6, 'fail'), general_chat: family(8, 'advisory') },
      }),
      evalRun('2026-11-01T09:05:00Z', { families: { tool_selection: family(9) } }),
    ];
    const rows = gateGrid(runColumns(runs));
    expect(rows.map(r => r.family)).toEqual(['general_chat', 'tool_selection']);
    expect(rows[0].cells.map(c => c.state)).toEqual(['not_run', 'advisory']);
    expect(rows[1].cells.map(c => c.state)).toEqual(['pass', 'fail']);
    expect(rows[1].cells[1].title).toContain('tool_selection: n 9, base 9, run 6, flips +0/-3, McNemar p 1.000, gate fail');
  });
});

describe('familyTrends', () => {
  it('flags a family under the ten-case floor with its n, and keeps a gap where it was not run', () => {
    const runs = [
      evalRun('2026-11-01T09:05:00Z', { families: { terminal: family(5, 'advisory', 6, 6) } }),
      evalRun('2026-11-02T09:05:00Z', { families: {} }),
    ];
    const [trend] = familyTrends(runColumns(runs));
    expect(trend).toMatchObject({ family: 'terminal', n: 6, underFloor: true, run: [5, null], base: [6, null] });
  });
});

describe('failedCases and latestByTrack', () => {
  it('lists failed cases with codes, and whether the baseline passed or lacks them', () => {
    const run = evalRun('2026-11-01T09:05:00Z', {
      cases: {
        'a.new': { pass: false, passes: 0, samples: 3, codes: ['format'] },
        'a.ok': evalCase(true),
        'a.regressed': evalCase(false, { codes: ['missing_value'] }),
      },
    });
    expect(failedCases(run).map(c => [c.caseId, c.regressed, c.newCase, c.codes])).toEqual([
      ['a.new', false, true, ['format']],
      ['a.regressed', true, false, ['missing_value']],
    ]);
  });

  it('picks the newest run of each track, untracked last', () => {
    const runs = [
      evalRun('2026-11-03T09:05:00Z'),
      evalRun('2026-11-02T16:00:00Z', { track: 'release' }),
      evalRun('2026-11-02T09:05:00Z'),
      evalRun('2026-10-30T12:00:00Z', { track: undefined }),
    ];
    expect(latestByTrack(runs).map(l => [l.track, l.run.startedAt])).toEqual([
      ['nightly', '2026-11-03T09:05:00Z'],
      ['release', '2026-11-02T16:00:00Z'],
      ['untracked', '2026-10-30T12:00:00Z'],
    ]);
  });
});
