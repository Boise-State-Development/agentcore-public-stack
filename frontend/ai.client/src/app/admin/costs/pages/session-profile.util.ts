import {
  CacheStatus,
  ContextTrajectoryPoint,
  DiagnosisSeverity,
  FeedbackProfile,
  SessionCostAnatomy,
  SessionProfile,
  TurnClass,
} from '../models';

/**
 * Pure helpers for the session profile band, the context-trajectory chart and
 * the diagnostic-JSON export. Kept out of the page so each is a one-line test.
 */

// ── severity ────────────────────────────────────────────────────────────────

export const SEVERITY_ORDER: Record<DiagnosisSeverity, number> = { high: 0, warn: 1, info: 2 };

export const SEVERITY_LABELS: Record<DiagnosisSeverity, string> = {
  high: 'High',
  warn: 'Warning',
  info: 'Info',
};

/** Chip classes for a diagnosis severity. Status colors, never series colors. */
export function severityChipClass(severity: DiagnosisSeverity): string {
  const base = 'inline-flex items-center gap-1 rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
  switch (severity) {
    case 'high':
      return `${base} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
    case 'warn':
      return `${base} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
    case 'info':
    default:
      return `${base} bg-state-info-100 text-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-300`;
  }
}

/** Dot classes for a row's top severity in the conversation list. */
export function severityDotClass(severity: DiagnosisSeverity | null | undefined): string {
  switch (severity) {
    case 'high':
      return 'bg-state-danger-500';
    case 'warn':
      return 'bg-state-warning-500';
    case 'info':
      return 'bg-state-info-500';
    default:
      return 'bg-gray-300 dark:bg-gray-600';
  }
}

// ── formatting ──────────────────────────────────────────────────────────────

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function formatTokensShort(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(tokens >= 100_000 ? 0 : 1)}K`;
  return tokens.toString();
}

/** A compact id for a session in a table cell: first 8 chars. */
export function shortSessionId(sessionId: string): string {
  return sessionId.length > 8 ? sessionId.slice(0, 8) : sessionId;
}

/** Render an evidence value for display: numbers with grouping, arrays joined. */
export function formatEvidenceValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    return Number.isInteger(value)
      ? new Intl.NumberFormat('en-US').format(value)
      : new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 }).format(value);
  }
  if (Array.isArray(value)) return value.map(String).join(', ');
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value);
}

/** `peakContextTokens` → "peakContextTokens" reads badly; space the camelCase. */
export function humanizeKey(key: string): string {
  return key
    .replace(/([a-z0-9])([A-Z])/g, (_, a: string, b: string) => `${a} ${b.toLowerCase()}`)
    .replace(/^./, (c) => c.toUpperCase());
}

// ── context trajectory chart ────────────────────────────────────────────────

export interface TrajectoryBar {
  point: ContextTrajectoryPoint;
  /** 0..1 share of the plot height. */
  height: number;
}

export interface TrajectoryScale {
  /** Y-axis maximum in tokens — clean-rounded above the tallest thing drawn. */
  yMax: number;
  bars: TrajectoryBar[];
  /** 0..1 position of the compaction threshold, or null if above yMax. */
  thresholdY: number | null;
  /** 0..1 position of the context window, or null if not drawn (usually far above). */
  windowY: number | null;
  /** Clean tick values for the y-axis, ascending. */
  ticks: number[];
}

/** Round up to a clean axis maximum (1/2/5 × 10^n). */
export function cleanCeiling(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * magnitude >= value) return step * magnitude;
  }
  return 10 * magnitude;
}

/**
 * Scale the trajectory so the tallest bar *and* the compaction threshold are
 * both in view — the threshold is the line the reader is judging the bars
 * against, so it must never be off the top. The context window is drawn only
 * when it is within 1.5× of the tallest bar; otherwise it would flatten every
 * ordinary conversation into a sliver and is named in the caption instead.
 */
export function scaleTrajectory(
  points: ContextTrajectoryPoint[],
  threshold: number,
  contextWindow: number | null | undefined,
): TrajectoryScale {
  const peak = points.reduce((m, p) => Math.max(m, p.contextTokens), 0);
  const drawWindow = !!contextWindow && contextWindow <= Math.max(peak, threshold) * 1.5;
  const tallest = Math.max(peak, threshold, drawWindow ? (contextWindow ?? 0) : 0);
  const yMax = cleanCeiling(tallest * 1.08);
  const bars = points.map((point) => ({ point, height: yMax > 0 ? point.contextTokens / yMax : 0 }));
  const tickStep = yMax / 4;
  const ticks = [0, 1, 2, 3, 4].map((i) => i * tickStep);
  return {
    yMax,
    bars,
    thresholdY: threshold <= yMax ? threshold / yMax : null,
    windowY: drawWindow && contextWindow ? contextWindow / yMax : null,
    ticks,
  };
}

/** Tailwind fill class for a trajectory bar by cache status. Mirrors the anatomy badges. */
export function trajectoryFillClass(status: CacheStatus | null | undefined): string {
  switch (status) {
    case 'hit':
      return 'fill-state-success-500';
    case 'first_write':
      return 'fill-state-info-500';
    case 'miss_ttl_expired':
      return 'fill-state-warning-500';
    case 'miss_avoidable':
      return 'fill-state-danger-500';
    case 'partial_miss':
      return 'fill-category-accent-partial-miss-500';
    case 'uncached':
    default:
      return 'fill-gray-400 dark:fill-gray-500';
  }
}

export const TRAJECTORY_LEGEND: ReadonlyArray<{ status: CacheStatus; label: string }> = [
  { status: 'hit', label: 'Hit' },
  { status: 'first_write', label: 'First write' },
  { status: 'partial_miss', label: 'Partial miss' },
  { status: 'miss_avoidable', label: 'Avoidable miss' },
  { status: 'miss_ttl_expired', label: 'TTL miss' },
  { status: 'uncached', label: 'Uncached' },
];

/** Only the legend entries that actually appear in the data. */
export function legendFor(points: ContextTrajectoryPoint[]): ReadonlyArray<{ status: CacheStatus; label: string }> {
  const present = new Set(points.map((p) => p.cacheStatus ?? 'uncached'));
  return TRAJECTORY_LEGEND.filter((e) => present.has(e.status));
}

// ── diagnostic JSON ─────────────────────────────────────────────────────────

/**
 * The payload a developer pastes into a model for a second opinion. Both
 * halves are content-free by construction (server-enforced), so the whole
 * object is safe to share. Includes a one-line preamble so the recipient
 * knows what it is looking at without the page.
 */
export function buildDiagnosticJson(
  profile: SessionProfile,
  anatomy: SessionCostAnatomy | null,
): string {
  return JSON.stringify(
    {
      _about:
        'Content-free cost diagnostic for one conversation on the AgentCore Public Stack. ' +
        'No user text or model output is included; numbers, tokens, hashes-as-counts, ' +
        'dollars, timestamps and tool/catalog ids only. Diagnose the dominant cost driver.',
      generatedAt: new Date().toISOString(),
      profile,
      anatomy,
    },
    null,
    2,
  );
}

// ── feedback ────────────────────────────────────────────────────────────────

export const TURN_CLASS_LABELS: Record<TurnClass, string> = {
  full: 'full',
  digestOnly: 'digest',
  retrieved: 'retrieved',
  none: 'no docs',
};

const TURN_CLASS_ORDER: TurnClass[] = ['full', 'digestOnly', 'retrieved', 'none'];

/** Down-thumb rate as a percentage, or null with nothing to rate. */
export function downRate(counts: { up: number; down: number }): number | null {
  const n = counts.up + counts.down;
  return n > 0 ? Math.round((counts.down / n) * 100) : null;
}

/**
 * One line of down-thumb rate per turn class, with n per class
 * (`full 50% of 4 · digest 0% of 2`). Classes with no thumbs are skipped;
 * null when the turn class is not tracked or nothing was thumbed.
 */
/** `2 retried · $0.35 rework`, or null when nothing was retried. */
export function feedbackRetryLine(feedback: FeedbackProfile | null | undefined): string | null {
  const retried = feedback?.retried ?? 0;
  if (retried === 0) return null;
  const parts = [`${retried} retried`];
  if (feedback?.reworkUsd != null) parts.push(`$${feedback.reworkUsd.toFixed(2)} rework`);
  return parts.join(' · ');
}

/** `3 copied · 1 continued` — implicit signals, kept off the thumbs line. */
export function feedbackImplicitLine(feedback: FeedbackProfile | null | undefined): string | null {
  const implicit = feedback?.implicit;
  if (!implicit) return null;
  const parts: string[] = [];
  if (implicit.copied > 0) parts.push(`${implicit.copied} copied`);
  if (implicit.continued > 0) parts.push(`${implicit.continued} continued`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/**
 * `judged 3 · Correctness 0.50 · tool failures 1/2 confirmed` — what the eval
 * sampler concluded. Evaluator names lose their `Builtin.` prefix; null when
 * nothing was judged.
 */
export function feedbackEvaluationsLine(feedback: FeedbackProfile | null | undefined): string | null {
  const ev = feedback?.evaluations;
  if (!ev || ev.judged === 0) return null;
  const parts = [`judged ${ev.judged}`];
  for (const [name, agg] of Object.entries(ev.byEvaluator ?? {}).sort(([a], [b]) => a.localeCompare(b))) {
    parts.push(`${name.replace(/^Builtin\./, '')} ${agg.mean.toFixed(2)}`);
  }
  if (ev.toolFailuresReported > 0) {
    parts.push(`tool failures ${ev.toolFailuresCorroborated}/${ev.toolFailuresReported} confirmed`);
  }
  return parts.join(' · ');
}

export function feedbackByTurnClassLine(feedback: FeedbackProfile | null | undefined): string | null {
  const by = feedback?.byTurnClass;
  if (!by) return null;
  const parts = TURN_CLASS_ORDER.flatMap((klass) => {
    const counts = by[klass];
    if (!counts) return [];
    const n = counts.up + counts.down;
    if (n === 0) return [];
    return [`${TURN_CLASS_LABELS[klass]} ${downRate(counts)}% of ${n}`];
  });
  return parts.length > 0 ? parts.join(' · ') : null;
}
