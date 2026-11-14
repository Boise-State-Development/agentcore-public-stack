import { LedgerEvalCase, LedgerEvalFamily, LedgerEvalRun, LedgerEvalsResponse } from '../app/admin/costs/models';

/**
 * `LEDGER#EVAL` rows as `GET /admin/costs/ledger/evals` exports them, shaped
 * like dev's nightly rows (eval-harness spec §16): one model, k=1, per-case
 * entries keyed by synthetic case id.
 */

export const HAIKU_55 = 'us.anthropic.claude-haiku-5-5';
export const HAIKU_45 = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';

export function family(run: number, gate: LedgerEvalFamily['gate'] = 'pass', n = 9, base = 9): LedgerEvalFamily {
  return { n, base, run, gained: 0, lost: Math.max(0, base - run), p: 1, gate, gating: gate !== 'advisory' };
}

export function evalCase(pass: boolean, extra: Partial<LedgerEvalCase> = {}): LedgerEvalCase {
  return { pass, passes: pass ? 1 : 0, samples: 1, base: true, ...extra };
}

export function evalRun(startedAt: string, overrides: Partial<LedgerEvalRun> = {}): LedgerEvalRun {
  return {
    startedAt,
    appVersion: '1.34.0',
    build: '99938194d6c2f17a',
    modelId: HAIKU_55,
    k: 1,
    target: 'dev',
    track: 'nightly',
    gateFailed: false,
    samples: 2,
    calls: 12,
    costUsd: 0.0075,
    durationS: 27.2,
    families: { tool_selection: family(9) },
    cases: {
      'tool_selection.weather_lookup': evalCase(true),
      'tool_selection.calendar_lookup': evalCase(true),
    },
    ...overrides,
  };
}

export function evalsResponse(runs: LedgerEvalRun[], overrides: Partial<LedgerEvalsResponse> = {}): LedgerEvalsResponse {
  const models = new Map<string, number>();
  for (const r of runs) if (r.modelId) models.set(r.modelId, (models.get(r.modelId) ?? 0) + 1);
  return {
    from: '2026-10-10',
    to: '2026-11-08',
    modelId: null,
    track: null,
    unit: 'build',
    runs,
    models: [...models].map(([modelId, n]) => ({ modelId, runs: n })).sort((a, b) => b.runs - a.runs),
    tracks: {},
    bands: [{ key: '1.34.0#99938194d6c2f17a', appVersion: '1.34.0', build: '99938194d6c2f17a', from: '2026-11-01', to: '2026-11-08' }],
    ...overrides,
  };
}
