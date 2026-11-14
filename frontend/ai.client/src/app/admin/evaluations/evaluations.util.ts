import {
  LedgerEvalCase,
  LedgerEvalFamily,
  LedgerEvalRun,
  LedgerEvalTrack,
} from '../costs/models';
import { formatUsd, shortBuild } from '../costs/releases/release-ledger.util';

/**
 * Pure derivations for the Evaluations page (docs/specs/eval-harness.md §16.3).
 *
 * Every function takes runs that are already one model's (D22): nothing here
 * averages across families or models, and the only numbers it produces are
 * counts read straight off the `LEDGER#EVAL` rows.
 */

export const TRACKS: readonly LedgerEvalTrack[] = ['nightly', 'release', 'manual', 'untracked'];

export const TRACK_LABELS: Record<LedgerEvalTrack, string> = {
  nightly: 'Nightly',
  release: 'Release',
  manual: 'Manual',
  untracked: 'Untracked',
};

/** D18: a gating family has at least ten cases; under it, the family is labelled with its `n`. */
export const FAMILY_FLOOR = 10;

/** A case is noisy when it flips at least this many times ... */
export const NOISY_FLIPS = 3;
/** ... within this many consecutive runs on one build (§16.3 item 4). */
export const NOISY_WINDOW = 10;

const DASH = '—';

export function trackOf(run: LedgerEvalRun): LedgerEvalTrack {
  return run.track ?? 'untracked';
}

/** A run's id on the page: its `startedAt`, which is also its ledger sort key. */
export function runId(run: LedgerEvalRun, index = 0): string {
  return run.startedAt ?? `run-${index}`;
}

/** `2026-11-07 16:14 UTC`. */
export function runWhen(run: LedgerEvalRun): string {
  return run.startedAt ? `${run.startedAt.slice(0, 16).replace('T', ' ')} UTC` : 'undated';
}

/** `1.34.0 · 99938194`. */
export function runBuild(run: LedgerEvalRun): string {
  if (!run.appVersion) return shortBuild(run.build);
  return run.build ? `${run.appVersion} · ${shortBuild(run.build)}` : run.appVersion;
}

export function formatDuration(seconds: number | undefined): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

export function formatCost(usd: number | undefined): string {
  return usd === undefined ? DASH : formatUsd(usd);
}

/** Cases a run scored: its per-case entries, else the families' `n` summed (a row from before EH-9). */
export function caseCount(run: LedgerEvalRun): number {
  const cases = Object.keys(run.cases ?? {}).length;
  if (cases) return cases;
  return Object.values(run.families ?? {}).reduce((sum, f) => sum + (f.n ?? 0), 0);
}

// ---------------------------------------------------------------------------
// Columns: one per run, oldest first
// ---------------------------------------------------------------------------

export interface RunColumn {
  id: string;
  /** `YYYY-MM-DD`, for the version bands. */
  date: string;
  /** `11-07`, the column header. */
  short: string;
  when: string;
  build: string;
  run: LedgerEvalRun;
}

export function runColumns(runs: readonly LedgerEvalRun[]): RunColumn[] {
  return runs
    .map((run, i) => ({ run, id: runId(run, i) }))
    .sort((a, b) => a.id.localeCompare(b.id))
    .map(({ run, id }) => ({
      id,
      date: (run.startedAt ?? '').slice(0, 10),
      short: (run.startedAt ?? '').slice(5, 10),
      when: runWhen(run),
      build: runBuild(run),
      run,
    }));
}

/** Every family any run covered, in a stable order. */
export function familiesOf(runs: readonly LedgerEvalRun[]): string[] {
  const names = new Set<string>();
  for (const run of runs) {
    for (const name of Object.keys(run.families ?? {})) names.add(name);
  }
  return [...names].sort();
}

// ---------------------------------------------------------------------------
// §8 rows
// ---------------------------------------------------------------------------

export interface FamilyRow {
  family: string;
  n: number;
  base: number;
  run: number;
  flips: string;
  p: string;
  gate: string;
  underFloor: boolean;
}

export function familyRow(family: string, f: LedgerEvalFamily): FamilyRow {
  return {
    family,
    n: f.n,
    base: f.base,
    run: f.run,
    flips: `+${f.gained ?? 0}/-${f.lost ?? 0}`,
    p: Number.isFinite(f.p) ? f.p.toFixed(3) : DASH,
    gate: f.gate,
    underFloor: (f.n ?? 0) < FAMILY_FLOOR,
  };
}

/** The run's §8 table, one row per family, in the page's family order. */
export function familyRows(run: LedgerEvalRun): FamilyRow[] {
  return Object.entries(run.families ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, f]) => familyRow(name, f));
}

/** One §8 row as a line: what a gate cell's hover shows. */
export function familyLine(row: FamilyRow): string {
  return `${row.family}: n ${row.n}, base ${row.base}, run ${row.run}, flips ${row.flips}, McNemar p ${row.p}, gate ${row.gate}`;
}

// ---------------------------------------------------------------------------
// Gate history (§16.3 item 2)
// ---------------------------------------------------------------------------

export type GateState = 'pass' | 'fail' | 'advisory' | 'not_run';

export interface GateCell {
  runId: string;
  state: GateState;
  /** The run and its §8 row, or "not run". */
  title: string;
}

export interface GateRow {
  family: string;
  cells: GateCell[];
}

export function gateState(f: LedgerEvalFamily | undefined): GateState {
  if (!f) return 'not_run';
  if (f.gate === 'fail') return 'fail';
  if (f.gate === 'pass') return 'pass';
  return 'advisory';
}

/** One row per family, one cell per column (oldest first). */
export function gateGrid(columns: readonly RunColumn[]): GateRow[] {
  return familiesOf(columns.map(c => c.run)).map(family => ({
    family,
    cells: columns.map(col => {
      const f = col.run.families?.[family];
      const state = gateState(f);
      return {
        runId: col.id,
        state,
        title: f ? `${col.when} · ${familyLine(familyRow(family, f))}` : `${col.when} · ${family}: not run`,
      };
    }),
  }));
}

// ---------------------------------------------------------------------------
// Family trends (§16.3 item 3)
// ---------------------------------------------------------------------------

export interface FamilyTrend {
  family: string;
  /** The newest run's `n`; the y axis runs to the largest `n` seen. */
  n: number;
  maxN: number;
  underFloor: boolean;
  /** Pass count per column, `null` where the family was not run. */
  run: (number | null)[];
  /** The baseline's pass count per column: flat unless a baseline was re-promoted. */
  base: (number | null)[];
  /** `n` per column, for the tooltip. */
  ns: (number | null)[];
}

export function familyTrends(columns: readonly RunColumn[]): FamilyTrend[] {
  return familiesOf(columns.map(c => c.run)).map(family => {
    const rows = columns.map(c => c.run.families?.[family]);
    const ns = rows.map(f => (f ? f.n : null));
    const present = ns.filter((n): n is number => n !== null);
    const n = present.length ? present[present.length - 1] : 0;
    return {
      family,
      n,
      maxN: present.length ? Math.max(...present) : 0,
      underFloor: n < FAMILY_FLOOR,
      run: rows.map(f => (f ? f.run : null)),
      base: rows.map(f => (f ? f.base : null)),
      ns,
    };
  });
}

// ---------------------------------------------------------------------------
// Case flip matrix (§16.3 item 4)
// ---------------------------------------------------------------------------

export type CaseState = 'pass' | 'fail' | 'absent';

export interface CaseCell {
  runId: string;
  state: CaseState;
  /** Failed against a baseline that passed. */
  regressed: boolean;
  title: string;
}

export interface CaseRow {
  caseId: string;
  /** The id without its family prefix. */
  label: string;
  noisy: boolean;
  cells: CaseCell[];
}

/** Outcome changes between consecutive outcomes. */
export function countFlips(outcomes: readonly boolean[]): number {
  let flips = 0;
  for (let i = 1; i < outcomes.length; i++) {
    if (outcomes[i] !== outcomes[i - 1]) flips++;
  }
  return flips;
}

/**
 * The noisy-case rule (§16.3 item 4): a case that flips at least
 * {@link NOISY_FLIPS} times within {@link NOISY_WINDOW} consecutive runs on
 * one build. Outcomes are grouped by build, so a change of build never counts
 * as a flip (that is a regression or a fix, which is what the gate is for),
 * and each build's runs are read chronologically in every window of ten.
 *
 * `outcomes` must be chronological; runs where the case was absent are left out.
 */
export function isNoisy(outcomes: readonly { build: string; pass: boolean }[]): boolean {
  const byBuild = new Map<string, boolean[]>();
  for (const o of outcomes) {
    const list = byBuild.get(o.build) ?? [];
    list.push(o.pass);
    byBuild.set(o.build, list);
  }
  for (const list of byBuild.values()) {
    const last = Math.max(0, list.length - NOISY_WINDOW);
    for (let start = 0; start <= last; start++) {
      if (countFlips(list.slice(start, start + NOISY_WINDOW)) >= NOISY_FLIPS) return true;
    }
  }
  return false;
}

function caseTitle(col: RunColumn, caseId: string, entry: LedgerEvalCase | undefined): string {
  if (!entry) return `${col.when} · ${caseId}: not in this run`;
  const parts = [
    `${col.when} · ${caseId}: ${entry.pass ? 'pass' : 'fail'} (${entry.passes}/${entry.samples} samples)`,
  ];
  if (entry.base !== undefined) parts.push(`baseline ${entry.base ? 'pass' : 'fail'}`);
  else parts.push('not in the baseline');
  if (entry.codes?.length) parts.push(`codes ${entry.codes.join(', ')}`);
  if (entry.names?.length) parts.push(`about ${entry.names.join(', ')}`);
  return parts.join(' · ');
}

/** One row per case of `family`, one cell per column (oldest first). */
export function flipMatrix(columns: readonly RunColumn[], family: string): CaseRow[] {
  const prefix = `${family}.`;
  const ids = new Set<string>();
  for (const col of columns) {
    for (const id of Object.keys(col.run.cases ?? {})) {
      if (id.startsWith(prefix)) ids.add(id);
    }
  }
  return [...ids].sort().map(caseId => {
    const outcomes: { build: string; pass: boolean }[] = [];
    const cells = columns.map(col => {
      const entry = col.run.cases?.[caseId];
      if (entry) outcomes.push({ build: col.run.build ?? '', pass: entry.pass });
      return {
        runId: col.id,
        state: (entry ? (entry.pass ? 'pass' : 'fail') : 'absent') as CaseState,
        regressed: !!entry && !entry.pass && entry.base === true,
        title: caseTitle(col, caseId, entry),
      };
    });
    return { caseId, label: caseId.slice(prefix.length), noisy: isNoisy(outcomes), cells };
  });
}

// ---------------------------------------------------------------------------
// Judge trends (§16.3 item 5)
// ---------------------------------------------------------------------------

export interface JudgeTrend {
  evaluator: string;
  run: (number | null)[];
  base: (number | null)[];
  ns: (number | null)[];
}

export function judgeTrends(columns: readonly RunColumn[], family: string): JudgeTrend[] {
  const evaluators = new Set<string>();
  for (const col of columns) {
    for (const name of Object.keys(col.run.judgeMeans?.[family] ?? {})) evaluators.add(name);
  }
  return [...evaluators].sort().map(evaluator => {
    const means = columns.map(col => col.run.judgeMeans?.[family]?.[evaluator]);
    return {
      evaluator,
      run: means.map(m => (m ? m.run : null)),
      base: means.map(m => (m ? m.base : null)),
      ns: means.map(m => (m ? m.n : null)),
    };
  });
}

// ---------------------------------------------------------------------------
// Run detail (§16.3 item 7)
// ---------------------------------------------------------------------------

export interface FailedCase {
  caseId: string;
  passes: number;
  samples: number;
  regressed: boolean;
  /** `true` where the baseline lacks the case. */
  newCase: boolean;
  codes: string[];
  names: string[];
}

export function failedCases(run: LedgerEvalRun): FailedCase[] {
  return Object.entries(run.cases ?? {})
    .filter(([, c]) => !c.pass)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([caseId, c]) => ({
      caseId,
      passes: c.passes,
      samples: c.samples,
      regressed: c.base === true,
      newCase: c.base === undefined,
      codes: c.codes ?? [],
      names: c.names ?? [],
    }));
}

/** The newest run of each track, from runs newest first. */
export function latestByTrack(runs: readonly LedgerEvalRun[]): { track: LedgerEvalTrack; run: LedgerEvalRun }[] {
  const seen = new Map<LedgerEvalTrack, LedgerEvalRun>();
  for (const run of runs) {
    const track = trackOf(run);
    if (!seen.has(track)) seen.set(track, run);
  }
  return TRACKS.filter(t => seen.has(t)).map(track => ({ track, run: seen.get(track)! }));
}
