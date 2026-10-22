import { describe, it, expect } from 'vitest';
import {
  armComparison,
  barWidthPercent,
  coverageSummary,
  dimensionLabel,
  rateLabel,
  reasonLabel,
} from './fleet-feedback.util';
import { FleetArm, FleetCoverage } from './fleet-feedback.models';

function arm(key: string, up: number, down: number, downRate: number | null): FleetArm {
  return { key, up, down, n: up + down, downRate, belowFloor: downRate === null };
}

function coverage(over: Partial<FleetCoverage> = {}): FleetCoverage {
  return {
    thumbs: 160, joined: 142, unjoined: 18, joinRate: 0.8875,
    sessionsWithFeedback: 89, sessionsJoined: 89, sessionsOmitted: 0, truncated: false,
    ...over,
  };
}

describe('fleet-feedback.util', () => {
  it('rateLabel shows a percentage, or says why there is none', () => {
    expect(rateLabel(arm('haiku', 30, 10, 0.25), 20)).toBe('25%');
    expect(rateLabel(arm('sonnet', 1, 1, null), 20)).toBe('n < 20');
    // Rounding is to whole points; nobody should read three decimals off a sampler.
    expect(rateLabel(arm('x', 2, 1, 0.3333), 1)).toBe('33%');
  });

  it('barWidthPercent is zero for an arm with no reportable rate', () => {
    expect(barWidthPercent(arm('a', 3, 1, 0.25))).toBe(25);
    expect(barWidthPercent(arm('b', 1, 0, null))).toBe(0);
  });

  it('armComparison reports the spread between the furthest-apart reportable arms', () => {
    const gap = armComparison([
      arm('haiku', 60, 40, 0.4),
      arm('sonnet', 90, 10, 0.1),
      arm('nova', 1, 1, null),
    ]);
    expect(gap).not.toBeNull();
    expect(gap!.best.key).toBe('sonnet');
    expect(gap!.worst.key).toBe('haiku');
    expect(gap!.gapPoints).toBe(30);
  });

  it('armComparison is null with fewer than two reportable arms', () => {
    expect(armComparison([])).toBeNull();
    expect(armComparison([arm('haiku', 60, 40, 0.4)])).toBeNull();
    expect(armComparison([arm('a', 1, 1, null), arm('b', 1, 1, null)])).toBeNull();
    // One reportable arm plus a below-floor one is still not a comparison.
    expect(armComparison([arm('a', 60, 40, 0.4), arm('b', 1, 1, null)])).toBeNull();
  });

  it('coverageSummary always states what the arms rest on, and flags a capped read', () => {
    expect(coverageSummary(coverage())).toBe('142 of 160 thumbs joined to a call · 89 conversations');
    expect(coverageSummary(coverage({ truncated: true, sessionsOmitted: 12 }))).toBe(
      '142 of 160 thumbs joined to a call · 89 conversations · 12 conversations beyond the join cap · window truncated',
    );
  });

  it('labels fall back to the raw key rather than rendering blank', () => {
    expect(dimensionLabel('model')).toBe('Model');
    expect(dimensionLabel('somethingNew')).toBe('somethingNew');
    expect(reasonLabel('tool_failed')).toBe('A tool failed');
    expect(reasonLabel('unspecified')).toBe('No reason given');
    expect(reasonLabel('brand_new_code')).toBe('brand_new_code');
  });
});
