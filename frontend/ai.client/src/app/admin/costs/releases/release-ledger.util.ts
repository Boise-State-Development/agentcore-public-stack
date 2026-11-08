import {
  LedgerBuild,
  LedgerDeployMarker,
  LedgerPair,
  LedgerSeries,
  LedgerUnitKind,
  LedgerVersion,
  LedgerVersionBand,
  LedgerVersionsResponse,
  ReadoutGroup,
  ReadoutTool,
} from '../models';

/**
 * Pure helpers for the Releases tab (docs/specs/release-ledger.md §6.4).
 *
 * Formatting and ordering only. Nothing here derives a KPI or a delta from
 * counters: the readout is the one place a comparison is computed (D10), so a
 * page and a script reading the same bundle can never disagree. Where the
 * readout gives a pair and no delta (the tools table), the tab prints the pair.
 */

export type KpiFormat = 'usd' | 'percent' | 'ms' | 'chars' | 'number';

/** Which way is an improvement. `neutral` KPIs describe the work, not its quality. */
export type KpiDirection = 'lower' | 'higher' | 'neutral';

export interface KpiDef {
  key: string;
  label: string;
  format: KpiFormat;
  better: KpiDirection;
  /** What `n` counts, for the "n" line under the pair. */
  nOf: string;
}

/**
 * The §6.2 KPIs in the order the cards render. Cost per turn is first and is
 * broken out per model under it (§6.4 item 3).
 */
export const KPI_DEFS: readonly KpiDef[] = [
  { key: 'costPerTurn', label: 'Cost per turn', format: 'usd', better: 'lower', nOf: 'turns' },
  { key: 'costPerCall', label: 'Cost per call', format: 'usd', better: 'lower', nOf: 'calls' },
  { key: 'cacheEfficiency', label: 'Cache efficiency', format: 'percent', better: 'higher', nOf: 'cached tokens' },
  { key: 'unexplainedWasteShare', label: 'Unexplained waste share', format: 'percent', better: 'lower', nOf: 'calls' },
  { key: 'partialMissShare', label: 'Partial-miss share of calls', format: 'percent', better: 'lower', nOf: 'calls' },
  { key: 'toolErrorRate', label: 'Tool error rate', format: 'percent', better: 'lower', nOf: 'tool calls' },
  { key: 'toolPayloadPerCall', label: 'Tool payload per call', format: 'chars', better: 'neutral', nOf: 'tool calls' },
  { key: 'documentTurnShare', label: 'Document turn share', format: 'percent', better: 'neutral', nOf: 'turns' },
  { key: 'meanFirstTokenMs', label: 'Mean model first byte', format: 'ms', better: 'lower', nOf: 'calls' },
  { key: 'meanTurnDurationMs', label: 'Mean turn duration', format: 'ms', better: 'lower', nOf: 'turns' },
  { key: 'emptyAnswerShare', label: 'Empty-answer share', format: 'percent', better: 'lower', nOf: 'turns with an outcome' },
  { key: 'interruptedShare', label: 'Interrupted share', format: 'percent', better: 'lower', nOf: 'turns with an outcome' },
  { key: 'pausedShare', label: 'Paused share', format: 'percent', better: 'neutral', nOf: 'turns with an outcome' },
  { key: 'forcedCompactionRate', label: 'Forced-compaction rate', format: 'percent', better: 'lower', nOf: 'compactions' },
];

const MINUS = '−';
const DASH = '—';

/** A KPI value in its unit; `null` (below its floor, or no denominator) is "—", never zero. */
export function formatKpiValue(value: number | null | undefined, format: KpiFormat): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  switch (format) {
    case 'usd':
      return formatUsd(value);
    case 'percent':
      return formatPercent(value);
    case 'ms':
      return `${Math.round(value).toLocaleString('en-US')} ms`;
    case 'chars':
      return `${Math.round(value).toLocaleString('en-US')} chars`;
    case 'number':
      return Math.round(value).toLocaleString('en-US');
  }
}

/**
 * Dollars at the precision the figure needs. A cost per turn is under a cent,
 * and `$0.01` beside `$0.01` would hide a 40% change.
 */
export function formatUsd(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return '$0.00';
  if (abs < 1) {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumSignificantDigits: 3,
      maximumSignificantDigits: 3,
    }).format(value);
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

export function formatPercent(share: number): string {
  const pct = share * 100;
  const digits = pct !== 0 && Math.abs(pct) < 1 ? 2 : 1;
  return `${pct.toFixed(digits)}%`;
}

/** A relative delta as a signed percentage; `null` (a side under its floor) is "—". */
export function formatDelta(delta: number | null | undefined): string {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) return DASH;
  const pct = delta * 100;
  const text = `${Math.abs(pct).toFixed(1)}%`;
  if (Math.abs(pct) < 0.05) return '0.0%';
  return pct > 0 ? `+${text}` : `${MINUS}${text}`;
}

export type DeltaTone = 'better' | 'worse' | 'neutral' | 'none';

/** Under half a percent either way is not called a direction. */
const TONE_EPSILON = 0.005;

export function deltaTone(delta: number | null | undefined, better: KpiDirection): DeltaTone {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) return 'none';
  if (better === 'neutral' || Math.abs(delta) < TONE_EPSILON) return 'neutral';
  const up = delta > 0;
  return (up && better === 'higher') || (!up && better === 'lower') ? 'better' : 'worse';
}

/** `n 956 / 117` — baseline then candidate, like every pair. */
export function formatN(n: LedgerPair<number> | undefined): string {
  if (!n) return `n ${DASH}`;
  return `n ${n[0].toLocaleString('en-US')} / ${n[1].toLocaleString('en-US')}`;
}

/** A content hash shortened to 8; a deploy-map label (`git-<sha12>`) keeps its prefix. */
export function shortBuild(build: string | null | undefined): string {
  if (!build) return '';
  return build.startsWith('git-') ? build.slice(0, 12) : build.slice(0, 8);
}

/** A unit key (`1.33.0` or `1.33.0#<build>`) for display. */
export function unitLabel(key: string | null | undefined): string {
  if (!key) return '';
  const [version, build] = key.split('#');
  return build ? `${version} · ${shortBuild(build)}` : version;
}

// ---------------------------------------------------------------------------
// Caveats
// ---------------------------------------------------------------------------

/**
 * Caveats that change how a number must be read, shown in amber. The rest
 * (floors, attribution, coverage of a measure) are notes, in grey.
 */
const WARNING_CAVEATS = new Set([
  'no_baseline',
  'mix_shift',
  'no_full_day',
  'deploy_in_window',
  'short_window',
  'unmetered_rows',
  'latency_unavailable',
  'latency_mixed_window',
  'coverage_truncated',
]);

export function caveatTone(code: string): 'warning' | 'note' {
  return WARNING_CAVEATS.has(code) ? 'warning' : 'note';
}

// ---------------------------------------------------------------------------
// Version strip
// ---------------------------------------------------------------------------

export interface UnitChip {
  key: string;
  label: string;
  sublabel: string;
  firstDate: string;
  lastDate: string;
  daysObserved: number;
  deployMarkers: LedgerDeployMarker[];
  /** `commit` builds are attributed by the deploy-time map, not their own stamp. */
  attributed: boolean;
}

/**
 * The strip's chips, oldest first. On dev the version is constant between
 * releases, so builds are the unit and the version is the label (§6.4 item 1).
 */
export function unitChips(response: LedgerVersionsResponse | null, unit: LedgerUnitKind): UnitChip[] {
  if (!response) return [];
  if (unit === 'build') return response.builds.map(buildChip);
  return response.versions.map(v => versionChip(v, response.builds));
}

function buildChip(b: LedgerBuild): UnitChip {
  return {
    key: `${b.appVersion}#${b.build}`,
    label: shortBuild(b.build),
    sublabel: b.appVersion,
    firstDate: b.firstDate,
    lastDate: b.lastDate,
    daysObserved: b.daysObserved,
    deployMarkers: b.deployMarkers,
    attributed: b.buildKind === 'commit',
  };
}

function versionChip(v: LedgerVersion, builds: LedgerBuild[]): UnitChip {
  const own = builds.filter(b => b.appVersion === v.appVersion);
  return {
    key: v.appVersion,
    label: v.appVersion,
    sublabel: `${v.builds} build${v.builds === 1 ? '' : 's'}`,
    firstDate: v.firstDate,
    lastDate: v.lastDate,
    daysObserved: v.daysObserved,
    deployMarkers: own.flatMap(b => b.deployMarkers),
    attributed: own.length > 0 && own.every(b => b.buildKind === 'commit'),
  };
}

/**
 * The unit to open on: versions when there are at least two to compare,
 * otherwise builds (dev between releases has one version and many builds).
 */
export function defaultUnit(response: LedgerVersionsResponse | null): LedgerUnitKind {
  return (response?.versions.length ?? 0) >= 2 ? 'version' : 'build';
}

// ---------------------------------------------------------------------------
// Trend chart
// ---------------------------------------------------------------------------

export interface TrendMetric {
  key: string;
  label: string;
  format: KpiFormat;
  /**
   * `sum`: an additive total, summed across units on a shared (deploy) day.
   * `kpi`: a ratio, read from the series point's own KPI; on a day two units
   * share it is left blank, because a rate across builds is neither build's.
   */
  kind: 'sum' | 'kpi';
}

export const TREND_METRICS: readonly TrendMetric[] = [
  { key: 'cost', label: 'Cost', format: 'usd', kind: 'sum' },
  { key: 'costPerTurn', label: 'Cost per turn', format: 'usd', kind: 'kpi' },
  { key: 'cacheEfficiency', label: 'Cache efficiency', format: 'percent', kind: 'kpi' },
  { key: 'unexplainedWasteShare', label: 'Unexplained waste share', format: 'percent', kind: 'kpi' },
  { key: 'partialMissShare', label: 'Partial-miss share', format: 'percent', kind: 'kpi' },
  { key: 'toolErrorRate', label: 'Tool error rate', format: 'percent', kind: 'kpi' },
  { key: 'meanFirstTokenMs', label: 'Mean model first byte', format: 'ms', kind: 'kpi' },
  { key: 'emptyAnswerShare', label: 'Empty-answer share', format: 'percent', kind: 'kpi' },
  { key: 'interruptedShare', label: 'Interrupted share', format: 'percent', kind: 'kpi' },
];

/** The shape `CostTrendsChartComponent` plots: a primary value (or a gap) and a call count. */
export interface TrendPoint {
  date: string;
  totalCost: number | null;
  totalRequests: number;
}

export function trendPoints(series: LedgerSeries[], metric: TrendMetric): TrendPoint[] {
  const byDate = new Map<string, { sum: number; calls: number; kpis: (number | null)[] }>();
  for (const s of series) {
    for (const p of s.points) {
      const day = byDate.get(p.date) ?? { sum: 0, calls: 0, kpis: [] };
      day.calls += p.totals['calls'] ?? 0;
      if (metric.kind === 'sum') day.sum += p.totals[metric.key] ?? 0;
      else day.kpis.push(p.kpis[metric.key]?.value ?? null);
      byDate.set(p.date, day);
    }
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, day]) => ({
      date,
      totalCost: metric.kind === 'sum' ? day.sum : day.kpis.length === 1 ? day.kpis[0] : null,
      totalRequests: day.calls,
    }));
}

export interface TrendBand {
  from: string;
  to: string;
  label: string;
}

export function trendBands(bands: LedgerVersionBand[]): TrendBand[] {
  return bands.map(b => ({ from: b.from, to: b.to, label: unitLabel(b.key) }));
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------

/** Sorted by the candidate's estimated input dollars, then the baseline's (§6.4 item 5). */
export function sortTools(tools: ReadoutTool[]): ReadoutTool[] {
  return [...tools].sort(
    (a, b) =>
      (b.estInputUsd[1] ?? 0) - (a.estInputUsd[1] ?? 0) ||
      (b.estInputUsd[0] ?? 0) - (a.estInputUsd[0] ?? 0) ||
      a.toolId.localeCompare(b.toolId),
  );
}

/** The top twenty by candidate cost, then baseline cost (§6.4 item 6). */
export function topGroups(groups: ReadoutGroup[], limit = 20): ReadoutGroup[] {
  return [...groups]
    .sort((a, b) => b.cost[1] - a.cost[1] || b.cost[0] - a.cost[0] || a.id.localeCompare(b.id))
    .slice(0, limit);
}

// ---------------------------------------------------------------------------
// Range
// ---------------------------------------------------------------------------

/** The last `days` UTC dates ending today, as the ledger routes take them. */
export function rangeEndingToday(days: number, today: Date): { from: string; to: string } {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - (days - 1));
  return { from: isoDate(start), to: isoDate(end) };
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}
