/** Fleet-level feedback attribution — response-feedback spec §7. */

/** One arm of one dimension. `downRate` is null below the coverage floor:
 *  the backend withholds the number rather than let a tiny sample be quoted. */
export interface FleetArm {
  key: string;
  up: number;
  down: number;
  n: number;
  downRate: number | null;
  belowFloor: boolean;
}

export interface FleetWindow {
  start: string;
  end: string;
  days: number;
}

/** How much of the window the arms rest on. Spec §9: a comparison ships with
 *  its coverage or it cannot be judged. */
export interface FleetCoverage {
  thumbs: number;
  joined: number;
  unjoined: number;
  joinRate: number | null;
  sessionsWithFeedback: number;
  sessionsJoined: number;
  sessionsOmitted: number;
  truncated: boolean;
}

/** Counts only. There is deliberately no fleet-wide rate here. */
export interface FleetTotals {
  up: number;
  down: number;
  thumbs: number;
}

export interface FleetFeedbackResponse {
  window: FleetWindow;
  totals: FleetTotals;
  coverage: FleetCoverage;
  arms: Record<string, FleetArm[]>;
  reasons: Record<string, number>;
  minimumN: number;
}
