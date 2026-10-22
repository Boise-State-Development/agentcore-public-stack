import { describe, it, expect } from 'vitest';
import {
  buildDiagnosticJson,
  cleanCeiling,
  downRate,
  feedbackByTurnClassLine,
  feedbackImplicitLine,
  feedbackEvaluationsLine,
  feedbackRetryLine,
  formatBytes,
  formatEvidenceValue,
  formatTokensShort,
  humanizeKey,
  legendFor,
  scaleTrajectory,
  severityChipClass,
  severityDotClass,
  shortSessionId,
  trajectoryFillClass,
} from './session-profile.util';
import { ContextTrajectoryPoint, SessionProfile } from '../models';

function point(callIndex: number, contextTokens: number, cacheStatus: ContextTrajectoryPoint['cacheStatus'] = 'hit'): ContextTrajectoryPoint {
  return { callIndex, timestamp: `2026-09-02T00:00:${callIndex}Z`, contextTokens, cacheStatus };
}

describe('session-profile.util', () => {
  describe('feedback', () => {
    it('downRate is a whole percentage, null with nothing to rate', () => {
      expect(downRate({ up: 0, down: 0 })).toBeNull();
      expect(downRate({ up: 3, down: 1 })).toBe(25);
      expect(downRate({ up: 0, down: 2 })).toBe(100);
    });

    it('feedbackByTurnClassLine reports rate and n per class, skipping empty classes', () => {
      const line = feedbackByTurnClassLine({
        up: 3,
        down: 2,
        byTurnClass: {
          full: { up: 1, down: 1 },
          digestOnly: { up: 2, down: 0 },
          retrieved: { up: 0, down: 0 },
          none: { up: 0, down: 1 },
        },
      });
      expect(line).toBe('full 50% of 2 · digest 0% of 2 · no docs 100% of 1');
    });

    it('feedbackRetryLine names retries and prices rework only when known', () => {
      expect(feedbackRetryLine({ up: 0, down: 1 })).toBeNull();
      expect(feedbackRetryLine({ up: 0, down: 1, retried: 1 })).toBe('1 retried');
      expect(feedbackRetryLine({ up: 0, down: 2, retried: 2, reworkUsd: 0.351 })).toBe('2 retried · $0.35 rework');
    });

    it('feedbackImplicitLine names implicit signals apart from the thumbs', () => {
      expect(feedbackImplicitLine({ up: 1, down: 0 })).toBeNull();
      expect(feedbackImplicitLine({ up: 1, down: 0, implicit: null })).toBeNull();
      expect(feedbackImplicitLine({ up: 1, down: 0, implicit: { copied: 0, continued: 0 } })).toBeNull();
      expect(feedbackImplicitLine({ up: 0, down: 0, implicit: { copied: 3, continued: 1 } })).toBe('3 copied · 1 continued');
    });

    it('feedbackEvaluationsLine reports judged means and corroboration, null when nothing judged', () => {
      expect(feedbackEvaluationsLine({ up: 0, down: 2 })).toBeNull();
      expect(feedbackEvaluationsLine({ up: 0, down: 2, evaluations: null })).toBeNull();
      expect(
        feedbackEvaluationsLine({
          up: 0,
          down: 3,
          evaluations: {
            judged: 3,
            byEvaluator: { 'Builtin.Faithfulness': { n: 1, mean: 0.5 }, 'Builtin.Correctness': { n: 2, mean: 0.25 } },
            toolFailuresReported: 2,
            toolFailuresCorroborated: 1,
          },
        }),
      ).toBe('judged 3 · Correctness 0.25 · Faithfulness 0.50 · tool failures 1/2 confirmed');
    });

    it('feedbackByTurnClassLine is null when the turn class is not tracked', () => {
      expect(feedbackByTurnClassLine({ up: 1, down: 1, byTurnClass: null })).toBeNull();
      expect(feedbackByTurnClassLine({ up: 1, down: 1 })).toBeNull();
      expect(feedbackByTurnClassLine(undefined)).toBeNull();
      expect(
        feedbackByTurnClassLine({
          up: 0,
          down: 0,
          byTurnClass: { full: { up: 0, down: 0 }, digestOnly: { up: 0, down: 0 }, retrieved: { up: 0, down: 0 }, none: { up: 0, down: 0 } },
        }),
      ).toBeNull();
    });
  });

  describe('cleanCeiling', () => {
    it('rounds up to a 1/2/2.5/5/10 step of the magnitude', () => {
      expect(cleanCeiling(0)).toBe(1);
      expect(cleanCeiling(7)).toBe(10);
      expect(cleanCeiling(12_000)).toBe(20_000);
      expect(cleanCeiling(21_000)).toBe(25_000);
      expect(cleanCeiling(40_000)).toBe(50_000);
      expect(cleanCeiling(108_000)).toBe(200_000);
    });
  });

  describe('scaleTrajectory', () => {
    it('keeps the compaction threshold in view even when every bar is below it', () => {
      const s = scaleTrajectory([point(0, 5_000), point(1, 8_000)], 100_000, 200_000);
      expect(s.thresholdY).not.toBeNull();
      expect(s.yMax).toBeGreaterThanOrEqual(100_000);
      // The window is 2× the tallest thing drawn → not drawn, named in the caption instead.
      expect(s.windowY).toBeNull();
    });

    it('draws the window when the conversation is close to it', () => {
      const s = scaleTrajectory([point(0, 150_000)], 100_000, 200_000);
      expect(s.windowY).not.toBeNull();
      expect(s.windowY!).toBeLessThanOrEqual(1);
      expect(s.thresholdY!).toBeLessThan(s.windowY!);
    });

    it('scales bar heights as a share of yMax and yields five ascending ticks', () => {
      const s = scaleTrajectory([point(0, 50_000)], 100_000, null);
      expect(s.bars[0].height).toBeCloseTo(50_000 / s.yMax, 6);
      expect(s.ticks).toHaveLength(5);
      expect(s.ticks[0]).toBe(0);
      expect(s.ticks[4]).toBe(s.yMax);
    });

    it('handles an empty trajectory', () => {
      const s = scaleTrajectory([], 100_000, 200_000);
      expect(s.bars).toEqual([]);
      expect(s.thresholdY).not.toBeNull();
    });
  });

  describe('legendFor', () => {
    it('lists only the statuses present, in canonical order', () => {
      const legend = legendFor([point(0, 1, 'partial_miss'), point(1, 1, 'hit'), point(2, 1, null)]);
      expect(legend.map((e) => e.status)).toEqual(['hit', 'partial_miss', 'uncached']);
    });
  });

  describe('fill and chip classes', () => {
    it('mirrors the anatomy badge palette', () => {
      expect(trajectoryFillClass('hit')).toContain('success');
      expect(trajectoryFillClass('partial_miss')).toContain('partial-miss');
      expect(trajectoryFillClass('miss_avoidable')).toContain('danger');
      expect(trajectoryFillClass(null)).toContain('gray');
      expect(severityChipClass('high')).toContain('danger');
      expect(severityChipClass('warn')).toContain('warning');
      expect(severityChipClass('info')).toContain('info');
      expect(severityDotClass(undefined)).toContain('gray');
    });
  });

  describe('formatting', () => {
    it('formats bytes across units', () => {
      expect(formatBytes(512)).toBe('512 B');
      expect(formatBytes(2_048)).toBe('2.0 KB');
      expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
      expect(formatBytes(3 * 1024 ** 3)).toBe('3.00 GB');
    });

    it('formats tokens compactly', () => {
      expect(formatTokensShort(999)).toBe('999');
      expect(formatTokensShort(8_400)).toBe('8.4K');
      expect(formatTokensShort(150_000)).toBe('150K');
      expect(formatTokensShort(1_200_000)).toBe('1.2M');
    });

    it('shortens session ids and humanizes camelCase keys', () => {
      expect(shortSessionId('bb7c0571-afec-481d')).toBe('bb7c0571');
      expect(shortSessionId('abc')).toBe('abc');
      expect(humanizeKey('peakContextTokens')).toBe('Peak context tokens');
      expect(humanizeKey('writeReadRatio')).toBe('Write read ratio');
    });

    it('renders evidence values for display', () => {
      expect(formatEvidenceValue(150_000)).toBe('150,000');
      expect(formatEvidenceValue(4.1234567)).toBe('4.1235');
      expect(formatEvidenceValue(['a', 'b'])).toBe('a, b');
      expect(formatEvidenceValue(null)).toBe('—');
      expect(formatEvidenceValue(true)).toBe('yes');
    });
  });

  describe('buildDiagnosticJson', () => {
    it('wraps profile and anatomy with a preamble and is valid JSON', () => {
      const profile = {
        sessionId: 's1',
        session: { sessionId: 's1', agentBound: false, costKnown: true, diagnosisCount: 0 },
        callCount: 0,
        compactionThreshold: 100_000,
        attachments: { count: 0, totalBytes: 0, byMime: {} },
        contextTrajectory: [],
        modelMix: {},
        fingerprintChanges: { systemPrompt: 0, toolConfig: 0, explainedByAgentSwitch: 0 },
        toolCensus: {},
        enabledToolIds: [],
        diagnoses: [],
        dataCoverage: { toolCensus: false, compactionCount: false, fingerprints: false, cost: true },
      } as SessionProfile;
      const parsed = JSON.parse(buildDiagnosticJson(profile, null));
      expect(parsed._about).toContain('Content-free');
      expect(parsed.profile.sessionId).toBe('s1');
      expect(parsed.anatomy).toBeNull();
      expect(typeof parsed.generatedAt).toBe('string');
    });
  });
});
