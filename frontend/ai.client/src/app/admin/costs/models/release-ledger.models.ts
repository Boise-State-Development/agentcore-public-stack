/**
 * Release ledger responses (`GET /admin/costs/ledger/*`, docs/specs/release-ledger.md §6.1, §6.3).
 *
 * Mirrors `backend/src/apis/app_api/admin/costs/ledger_models.py`, by alias.
 * Content-free by the D7 contract: ids, dates and numbers. Every KPI is
 * `{value, n}` with `value: null` below its floor, never `0`. Every readout
 * pair is `[baseline, candidate]`.
 */

/** `version` compares every build of an appVersion together; `build` each build apart. */
export type LedgerUnitKind = 'version' | 'build';

export interface LedgerKpi {
  value: number | null;
  n: number;
}

export type LedgerKpis = Record<string, LedgerKpi>;

export interface LedgerDeployMarker {
  service: string;
  firstSeenAt: string;
}

export interface LedgerBuild {
  appVersion: string;
  build: string;
  /** `image` (a stamped content hash), `commit` (a backfill label from the deploy map) or `unknown`. */
  buildKind: 'image' | 'commit' | 'unknown' | string;
  firstDate: string;
  lastDate: string;
  daysObserved: number;
  firstLiveAt: string | null;
  backfilled: boolean;
  deployMarkers: LedgerDeployMarker[];
  totals: Record<string, number>;
  kpis: LedgerKpis;
}

export interface LedgerVersion {
  appVersion: string;
  firstDate: string;
  lastDate: string;
  daysObserved: number;
  builds: number;
  firstLiveAt: string | null;
  totals: Record<string, number>;
  kpis: LedgerKpis;
}

export interface LedgerBackfillMarker {
  from: string;
  to: string;
  rows: number;
  ledgerRows: number;
  status: string;
  startedAt: string | null;
  completedAt: string | null;
}

export interface LedgerVersionsResponse {
  from: string;
  to: string;
  versions: LedgerVersion[];
  builds: LedgerBuild[];
  backfills: LedgerBackfillMarker[];
}

export interface LedgerSeriesPoint {
  date: string;
  totals: Record<string, number>;
  kpis: LedgerKpis;
}

export interface LedgerSeries {
  key: string;
  points: LedgerSeriesPoint[];
}

export interface LedgerVersionBand {
  key: string;
  appVersion: string;
  build: string | null;
  from: string;
  to: string;
}

export interface LedgerSeriesResponse {
  from: string;
  to: string;
  by: 'version' | 'model' | 'tool' | 'agent' | 'project';
  unit: LedgerUnitKind;
  series: LedgerSeries[];
  bands: LedgerVersionBand[];
  omittedKeys: number;
}

export interface LedgerTool {
  toolId: string;
  totals: Record<string, number>;
  kpis: LedgerKpis;
  payloadMeasured: boolean;
  specTokens: number | null;
}

export interface LedgerToolsResponse {
  from: string;
  to: string;
  version: string | null;
  tools: LedgerTool[];
  /** Tool-table columns the census does not record yet (e.g. the awaiting-user share). */
  notMeasured: string[];
}

export interface LedgerThumbs {
  up: number;
  down: number;
  n: number;
  downRate: number | null;
  belowFloor: boolean;
  retries: number;
}

export interface LedgerProvenance {
  verdicts: number;
  claims: number;
  supported: number;
  quoteUnsupported: number;
  unassessed: number;
  unverified: number;
  quoteUnsupportedShare: LedgerKpi;
}

/** A `LEDGER#EVAL` row as published (PR-5): §8's report table, nothing from any answer. */
export interface LedgerEvalFamily {
  n: number;
  base: number;
  run: number;
  gained: number;
  lost: number;
  p: number;
  gate: 'pass' | 'fail' | 'advisory' | string;
  gating?: boolean;
}

/**
 * One case's outcome on a `LEDGER#EVAL` row (eval-harness spec §16 D20): a
 * synthetic case id's majority outcome, counts and closed failure codes.
 * `base` is the baseline's outcome, absent for a case the baseline lacks.
 */
export interface LedgerEvalCase {
  pass: boolean;
  passes: number;
  samples: number;
  base?: boolean;
  codes?: string[];
  names?: string[];
}

/** A judge's mean for one family and evaluator, with the baseline's. */
export interface LedgerEvalJudgeMean {
  n: number;
  base: number;
  run: number;
}

/** Which pass a run was; `untracked` is a row published before `--track` existed. */
export type LedgerEvalTrack = 'nightly' | 'release' | 'manual' | 'untracked';

export interface LedgerEvalRun {
  startedAt?: string;
  appVersion?: string;
  build?: string;
  modelId?: string;
  k?: number;
  target?: string;
  gitCommit?: string;
  families?: Record<string, LedgerEvalFamily>;
  gateFailed?: boolean;
  samples?: number;
  costUsd?: number;
  durationS?: number;
  calls?: number;
  track?: Exclude<LedgerEvalTrack, 'untracked'>;
  cases?: Record<string, LedgerEvalCase>;
  judgeMeans?: Record<string, Record<string, LedgerEvalJudgeMean>>;
  judgeModel?: string;
  otherBuildCalls?: number;
}

/** `GET /admin/costs/ledger/evals` (eval-harness spec §16 D21): runs newest first. */
export interface LedgerEvalsResponse {
  from: string;
  to: string;
  modelId: string | null;
  track: LedgerEvalTrack | null;
  unit: LedgerUnitKind;
  runs: LedgerEvalRun[];
  /** Every model with a run in the range, before the model and track filters, most runs first. */
  models: { modelId: string; runs: number }[];
  /** Runs per track in the range, before the filters. */
  tracks: Partial<Record<LedgerEvalTrack, number>>;
  bands: LedgerVersionBand[];
}

export interface LedgerEvalsOptions extends LedgerRangeOptions {
  modelId?: string;
  track?: LedgerEvalTrack;
  unit?: LedgerUnitKind;
}

/** A `LEDGER#SMOKE` row as published (PR-5). */
export interface LedgerSmokeRun {
  startedAt?: string;
  appVersion?: string;
  build?: string;
  target?: string;
  pass?: number;
  fail?: number;
  skip?: number;
  observed?: number;
  firstTokenMsMedian?: number;
  secondTurnCacheRead?: number;
  secondTurnCacheWrite?: number;
  rows?: Record<string, string>;
}

export interface LedgerQualityUnit {
  key: string;
  appVersion: string;
  build: string | null;
  firstDate: string;
  lastDate: string;
  kpis: LedgerKpis;
  outcomes: Record<string, number>;
  eval: LedgerEvalRun[];
  smoke: LedgerSmokeRun[];
  thumbs: LedgerThumbs;
  provenance: LedgerProvenance;
}

export interface LedgerQualityResponse {
  from: string;
  to: string;
  unit: LedgerUnitKind;
  minimumN: number;
  units: LedgerQualityUnit[];
  thumbsCoverage: Record<string, unknown>;
  provenanceCoverage: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Readout (§6.3)
// ---------------------------------------------------------------------------

/** `[baseline, candidate]`. */
export type LedgerPair<T> = [T, T];

export interface ReadoutSide {
  key: string;
  appVersion: string;
  build: string | null;
  builds: number;
  window: [string | null, string | null];
  days: number;
  fullDays: number;
}

export interface ReadoutKpi {
  baseline: number | null;
  candidate: number | null;
  /** Relative: `(candidate - baseline) / baseline`. `null` when either side is under its floor. */
  delta: number | null;
  n: LedgerPair<number>;
  byModel?: Record<string, ReadoutKpi> | null;
}

export interface ReadoutMixShift {
  warn: boolean;
  models: Record<string, LedgerPair<number>>;
  activeUsers: LedgerPair<number | null>;
}

export interface ReadoutTool {
  toolId: string;
  calls: LedgerPair<number>;
  errorRate: LedgerPair<number | null>;
  resultCharsPerCall: LedgerPair<number | null>;
  meanDurationMs: LedgerPair<number | null>;
  estInputTokens: LedgerPair<number | null>;
  estInputUsd: LedgerPair<number | null>;
  payloadMeasured: LedgerPair<boolean | null>;
  specTokens: number | null;
}

export interface ReadoutGroup {
  id: string;
  calls: LedgerPair<number>;
  turns: LedgerPair<number>;
  cost: LedgerPair<number>;
  costPerTurn: ReadoutKpi;
  wastePerTurn: ReadoutKpi;
  toolErrorRate: LedgerPair<number | null>;
}

export interface ReadoutLatencyMetric {
  p50: LedgerPair<number | null>;
  p90: LedgerPair<number | null>;
  n: LedgerPair<number>;
}

export interface ReadoutLatency {
  available: boolean;
  serverFirstTokenMs?: ReadoutLatencyMetric | null;
  clientFirstTokenMs?: ReadoutLatencyMetric | null;
  probeColdFirstTokenMs?: ReadoutLatencyMetric | null;
  probeFollowUpFirstTokenMs?: ReadoutLatencyMetric | null;
}

export interface ReadoutPairs<T> {
  baseline: T[];
  candidate: T[];
}

export interface ReadoutThumbs {
  up: LedgerPair<number>;
  down: LedgerPair<number>;
  n: LedgerPair<number>;
  downRate: LedgerPair<number | null>;
  retries: LedgerPair<number>;
  minimumN: number;
  coverage?: Record<string, unknown> | null;
}

export interface ReadoutProvenance {
  claims: LedgerPair<number>;
  quoteUnsupported: LedgerPair<number>;
  quoteUnsupportedShare: LedgerPair<number | null>;
  coverage?: Record<string, unknown> | null;
}

export interface ReadoutQuality {
  eval: ReadoutPairs<LedgerEvalRun>;
  smoke: ReadoutPairs<LedgerSmokeRun>;
  emptyAnswerShare: LedgerPair<number | null>;
  interruptedShare: LedgerPair<number | null>;
  pausedShare: LedgerPair<number | null>;
  forcedCompactionRate: LedgerPair<number | null>;
  toolErrorRate: LedgerPair<number | null>;
  outcomes: Record<string, LedgerPair<number>>;
  provenance: ReadoutProvenance;
  thumbs: ReadoutThumbs;
}

export interface ReadoutPlatform {
  month: string;
  available: boolean;
  inferenceCost: number;
  platformCost: number;
  totalCost: number;
}

/** One generated warning, its `code` from the fixed `CAVEAT_CODES` set. */
export interface ReadoutCaveat {
  code: string;
  message: string;
}

export interface LedgerReadout {
  candidate: ReadoutSide;
  baseline: ReadoutSide | null;
  unit: LedgerUnitKind;
  requestedDays: number;
  mixShift: ReadoutMixShift;
  kpis: Record<string, ReadoutKpi>;
  tools: ReadoutTool[];
  agents: ReadoutGroup[];
  projects: ReadoutGroup[];
  latency: ReadoutLatency;
  quality: ReadoutQuality;
  platform: ReadoutPlatform | null;
  caveats: ReadoutCaveat[];
}

export interface LedgerRangeOptions {
  from?: string;
  to?: string;
}

export interface LedgerReadoutOptions {
  version?: string;
  baseline?: string;
  days?: number;
}
