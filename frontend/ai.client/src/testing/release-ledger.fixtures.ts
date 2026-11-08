import { LedgerReadout, LedgerVersionsResponse, ReadoutKpi } from '../app/admin/costs/models';

/** Test fixtures shaped like dev's responses on 2026-11-07 (spec §6.6), ids only. */

const kpi = (baseline: number | null, candidate: number | null, delta: number | null, n: [number, number]): ReadoutKpi => ({
  baseline,
  candidate,
  delta,
  n,
});

export function readoutFixture(overrides: Partial<LedgerReadout> = {}): LedgerReadout {
  return {
    candidate: {
      key: '1.33.0#d5ead33e05f3e229',
      appVersion: '1.33.0',
      build: 'd5ead33e05f3e229',
      builds: 1,
      window: ['2026-11-07', '2026-11-07'],
      days: 1,
      fullDays: 0,
    },
    baseline: {
      key: '1.33.0#f200a6a3514248d3',
      appVersion: '1.33.0',
      build: 'f200a6a3514248d3',
      builds: 1,
      window: ['2026-11-07', '2026-11-07'],
      days: 1,
      fullDays: 0,
    },
    unit: 'build',
    requestedDays: 7,
    mixShift: {
      warn: true,
      models: { 'us.anthropic.claude-haiku-4-5': [0.51, 0], 'us.anthropic.claude-haiku-5-5': [0.45, 0.92] },
      activeUsers: [4, 3],
    },
    kpis: {
      costPerTurn: {
        ...kpi(0.00865, 0.01226, 0.4173, [956, 117]),
        byModel: { 'us.anthropic.claude-haiku-5-5': kpi(0.009, 0.0122, 0.3556, [430, 110]) },
      },
      cacheEfficiency: kpi(0.91, 0.88, -0.033, [120000, 9000]),
      emptyAnswerShare: kpi(0.004, null, null, [900, 14]),
      meanFirstTokenMs: kpi(1810, 1790, -0.011, [956, 117]),
    },
    tools: [
      {
        toolId: 'fetch_url_content',
        calls: [12, 30],
        errorRate: [null, 0.1],
        resultCharsPerCall: [null, 6100],
        meanDurationMs: [null, 820],
        estInputTokens: [null, 45750],
        estInputUsd: [null, 0.05],
        payloadMeasured: [false, true],
        specTokens: 310,
      },
      {
        toolId: 'browse_web',
        calls: [100, 120],
        errorRate: [0.04, 0.09],
        resultCharsPerCall: [6100, 6050],
        meanDurationMs: [2100, 2300],
        estInputTokens: [152500, 181500],
        estInputUsd: [3.21, 3.44],
        payloadMeasured: [true, true],
        specTokens: null,
      },
    ],
    agents: [],
    projects: [],
    latency: {
      available: true,
      serverFirstTokenMs: { p50: [2030, 2714], p90: [4100, 4650], n: [950, 110] },
    },
    quality: {
      eval: {
        baseline: [],
        candidate: [
          {
            startedAt: '2026-11-07T16:14:56.397504Z',
            modelId: 'us.anthropic.claude-haiku-5-5',
            target: 'dev',
            gateFailed: false,
            families: { tool_selection: { n: 9, base: 9, run: 9, gained: 0, lost: 0, p: 1, gate: 'pass' } },
          },
        ],
      },
      smoke: {
        baseline: [],
        candidate: [{ startedAt: '2026-11-07T16:16:00.226929Z', pass: 11, fail: 0, skip: 6, observed: 1, firstTokenMsMedian: 1800 }],
      },
      emptyAnswerShare: [0.004, null],
      interruptedShare: [0.02, null],
      pausedShare: [0.01, null],
      forcedCompactionRate: [null, null],
      toolErrorRate: [0.05, 0.07],
      outcomes: { completed: [890, 13], outcomeTurns: [900, 14] },
      provenance: { claims: [55, 9], quoteUnsupported: [6, 1], quoteUnsupportedShare: [0.109, null] },
      thumbs: { up: [3, 1], down: [1, 0], n: [4, 1], downRate: [null, null], retries: [0, 0], minimumN: 20 },
    },
    platform: null,
    caveats: [
      { code: 'no_full_day', message: 'candidate 1.33.0#d5ead33e05f3e229 has no full day' },
      { code: 'thumbs_below_floor', message: 'candidate thumbs arm under the floor (n=1): counts only, no rate' },
    ],
    ...overrides,
  };
}

export function versionsFixture(): LedgerVersionsResponse {
  const build = (b: string, first: string, last: string, kind: string) => ({
    appVersion: '1.33.0',
    build: b,
    buildKind: kind,
    firstDate: first,
    lastDate: last,
    daysObserved: 1,
    firstLiveAt: null,
    backfilled: kind === 'commit',
    deployMarkers: kind === 'image' ? [{ service: 'runtime', firstSeenAt: `${first}T20:00:00Z` }] : [],
    totals: {},
    kpis: {},
  });
  return {
    from: '2026-10-09',
    to: '2026-11-07',
    versions: [
      { appVersion: '1.33.0', firstDate: '2026-11-05', lastDate: '2026-11-07', daysObserved: 3, builds: 3, firstLiveAt: null, totals: {}, kpis: {} },
    ],
    builds: [
      build('git-0123456789ab', '2026-11-05', '2026-11-05', 'commit'),
      build('f200a6a3514248d3', '2026-11-06', '2026-11-07', 'image'),
      build('d5ead33e05f3e229', '2026-11-07', '2026-11-07', 'image'),
    ],
    backfills: [],
  };
}
