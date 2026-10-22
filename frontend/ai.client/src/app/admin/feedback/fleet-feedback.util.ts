import { FleetArm, FleetCoverage } from './fleet-feedback.models';

/**
 * Pure helpers for the fleet feedback page. Kept out of the component so the
 * honesty rules of response-feedback §9 — no rate below the floor, always a
 * comparison rather than a score — are each a one-line test.
 */

export const DIMENSION_LABELS: Record<string, string> = {
  model: 'Model',
  agentSwitch: 'Agent switch',
  callsSinceCompaction: 'Distance from a compaction cut',
  turnClass: 'Document turn class',
};

export const DIMENSION_HINTS: Record<string, string> = {
  model: 'Is a cheaper model costing answer quality on this workload?',
  agentSwitch: 'An @-mention forks the history. Does it also cost quality?',
  callsSinceCompaction: 'Does quality drop after history is summarised away?',
  turnClass: 'Full document in context, a digest only, or pages retrieved back.',
};

export const REASON_LABELS: Record<string, string> = {
  wrong: 'Wrong or made up',
  instructions: 'Ignored instructions',
  length: 'Too long / short',
  tool_failed: 'A tool failed',
  outdated: 'Out of date',
  other: 'Something else',
  unspecified: 'No reason given',
};

export function dimensionLabel(key: string): string {
  return DIMENSION_LABELS[key] ?? key;
}

export function reasonLabel(key: string): string {
  return REASON_LABELS[key] ?? key;
}

/** `31%`, or why there is no number. */
export function rateLabel(arm: FleetArm, minimumN: number): string {
  if (arm.downRate === null) return `n < ${minimumN}`;
  return `${Math.round(arm.downRate * 100)}%`;
}

/** Bar width for a reportable rate; 0 when the arm is below the floor. */
export function barWidthPercent(arm: FleetArm): number {
  return arm.downRate === null ? 0 : Math.round(arm.downRate * 100);
}

export interface ArmComparison {
  best: FleetArm;
  worst: FleetArm;
  gapPoints: number;
}

/**
 * The comparison §9 asks for: the reportable arms furthest apart, and by how
 * much. Null with fewer than two reportable arms, which is the common case
 * early on and must read as "not enough data" rather than as a finding.
 */
export function armComparison(arms: FleetArm[]): ArmComparison | null {
  const reportable = arms.filter(a => a.downRate !== null);
  if (reportable.length < 2) return null;
  const sorted = [...reportable].sort((a, b) => (a.downRate ?? 0) - (b.downRate ?? 0));
  const best = sorted[0];
  const worst = sorted[sorted.length - 1];
  const gapPoints = Math.round(((worst.downRate ?? 0) - (best.downRate ?? 0)) * 100);
  return { best, worst, gapPoints };
}

/** `142 of 160 thumbs joined · 89 conversations`, plus any truncation note. */
export function coverageSummary(coverage: FleetCoverage): string {
  const parts = [`${coverage.joined} of ${coverage.thumbs} thumbs joined to a call`];
  parts.push(`${coverage.sessionsWithFeedback} conversations`);
  if (coverage.sessionsOmitted > 0) {
    parts.push(`${coverage.sessionsOmitted} conversations beyond the join cap`);
  }
  if (coverage.truncated) parts.push('window truncated');
  return parts.join(' · ');
}
