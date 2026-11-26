/**
 * Front-end models for scheduled agent runs (`/schedules/*`).
 *
 * Mirrors the backend contract from `apis/app_api/schedules/` — field names
 * are the camelCase aliases the backend serializes. Bounded cadence set
 * only (daily / weekday / weekly) — no cron UI, per
 * docs/specs/scheduled-agent-runs.md §3.
 */

export type ScheduleCadence = 'daily' | 'weekday' | 'weekly' | 'interval';
export type IntervalUnit = 'minutes' | 'hours';
export type ScheduledPromptState = 'active' | 'paused' | 'paused_error';
/**
 * What a schedule's runs do (docs/specs/quiet-watcher.md §3.1): `standard` runs
 * and delivers every time; `watch` checks and only leaves something behind when
 * it finds something new.
 */
export type ScheduleMode = 'standard' | 'watch';

/** Floor for the custom "every N" cadence (mirrors backend MIN_INTERVAL_MINUTES). */
export const MIN_INTERVAL_MINUTES = 15;

/** Public view of a scheduled prompt (backend ScheduledPromptResponse). */
export interface ScheduledPrompt {
  scheduleId: string;
  assistantId?: string | null;
  label: string;
  promptText: string;
  cadence: ScheduleCadence;
  hourLocal: number;
  weekday?: number | null;
  intervalValue?: number | null;
  intervalUnit?: IntervalUnit | null;
  timezone: string;
  state: ScheduledPromptState;
  stateReason?: string | null;
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastRunStatus?: string | null;
  lastRunSessionId?: string | null;
  lastError?: string | null;
  runsToday: number;
  maxRunsPerDay: number;
  enabledTools?: string[] | null;
  deliverEmail: boolean;
  /**
   * Set for a schedule that runs in a Shared Project (3.2). It is changed and
   * resumed from the project; here it can only be paused or deleted.
   */
  projectId?: string | null;
  /** A project schedule's delivery: the creator's sidebar, or shared with the project. */
  deliverTo?: 'owner' | 'project' | null;
  /** `watch` for a quiet watcher; absent from an older backend, which means `standard`. */
  mode?: ScheduleMode;
  /** The model each run uses; null is the default (the Agent's or the user's). */
  modelId?: string | null;
  createdAt: string;
  updatedAt: string;
}

/** Request body for POST /schedules. */
export interface CreateScheduleRequest {
  label: string;
  promptText: string;
  cadence: ScheduleCadence;
  hourLocal: number;
  weekday?: number | null;
  intervalValue?: number | null;
  intervalUnit?: IntervalUnit | null;
  timezone: string;
  assistantId?: string | null;
  enabledTools?: string[] | null;
  deliverEmail?: boolean;
  /** Sent only while `features.proactiveRuns` is on; the backend refuses it (422) while off. */
  mode?: ScheduleMode;
  /** Sent only while `features.proactiveRuns` is on. Omitted for the default model. */
  modelId?: string;
}

/**
 * Request body for PATCH /schedules/{id}.
 *
 * The backend's `update_scheduled_prompt` treats `None`/absent fields as
 * "leave unchanged" (it only appends a SET clause when a value is not
 * None) — so there is currently NO way to send a null over the wire and
 * have it clear `assistantId` or `enabledTools`. The form works around
 * this client-side with explicit "clear" checkboxes that omit the field
 * from the outgoing JSON instead of sending null (same effective
 * no-op from the backend's point of view) — see
 * ScheduleFormPage.buildUpdateRequest. A real "detach assistant" /
 * "reset tools to defaults" action requires a backend follow-up (a
 * sentinel value or a separate PATCH endpoint) — flagged in the PR.
 */
export interface UpdateScheduleRequest {
  label?: string;
  promptText?: string;
  cadence?: ScheduleCadence;
  hourLocal?: number;
  weekday?: number | null;
  intervalValue?: number | null;
  intervalUnit?: IntervalUnit | null;
  timezone?: string;
  assistantId?: string | null;
  enabledTools?: string[] | null;
  deliverEmail?: boolean;
  state?: 'active' | 'paused';
  // A bare null cannot clear assistantId/enabledTools (the backend reads null
  // as "leave unchanged"), so clearing is an explicit intent. clearTools
  // re-snapshots the caller's current RBAC-allowed tools; clearAssistant
  // reverts to the default agent. Not combinable with the value field.
  clearAssistant?: boolean;
  clearTools?: boolean;
  /** The quiet-watcher fields, sent only while `features.proactiveRuns` is on. */
  mode?: ScheduleMode;
  modelId?: string;
  /** Revert to the default model. Not combinable with `modelId`. */
  clearModel?: boolean;
}

/**
 * How one run of a personal schedule ended (`ScheduleRunStatus`, quiet-watcher
 * §3.4). `quiet` and `notify` are a watcher's: nothing new, or found something.
 */
export type ScheduleRunStatus = 'completed' | 'error' | 'timeout' | 'paused' | 'quiet' | 'notify' | 'needs_user';

/** One run of a personal schedule (backend `ScheduleRun`). */
export interface ScheduleRun {
  runId: string;
  scheduleId: string;
  /** A `ScheduleRunStatus`; typed loosely so a status this build doesn't know still renders. */
  status: ScheduleRunStatus | string;
  /** How many new findings a `notify` run reported. */
  itemCount: number;
  /** The run's conversation; absent for a quiet run, whose conversation is removed. */
  sessionId?: string | null;
  finishedAt: string;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  /** Written to the prompt cache; most of a short run's cost. Absent on rows from before it was recorded. */
  cacheWriteTokens?: number;
  /** A `needs_user` run's pause: when it expires and how many prompts it holds (answer-from-inbox §3.8). */
  expiresAt?: string | null;
  promptCount?: number;
  /** How its pause was answered from the inbox: `approved`, `declined`, `answered`, `skipped`. */
  resolution?: string | null;
  /** What the resumed run then did: `finished`, `waiting_again`, `failed`. */
  resumeOutcome?: string | null;
  resumeRunId?: string | null;
  resumedAt?: string | null;
}

/** Response from GET /schedules/{id}/runs: the newest 50, newest first. */
export interface ScheduleRunsResponse {
  runs: ScheduleRun[];
}

/** What a watcher carries from one run to the next (backend `WatchState`, quiet-watcher §3.2). */
export interface WatchState {
  /** Where the next run checks from, by name (written by the model). */
  cursors: Record<string, string>;
  /** What the watcher is keeping an eye on (written by the model). */
  tracking: string[];
  /** Finding keys already notified, newest last. Shown only as a count. */
  seen: string[];
  version: number;
  lastRunAt?: string | null;
  updatedAt?: string | null;
  /** Set while the watcher holds on a run that waits on the user (answer-from-inbox §3.2). */
  pausedSessionId?: string | null;
  pausedUntil?: string | null;
}

/** Response from GET /schedules/{id}/state; `state` is null when the watcher has none. */
export interface WatchStateResponse {
  state: WatchState | null;
}

/** Response from GET /schedules. */
export interface ScheduledPromptsListResponse {
  schedules: ScheduledPrompt[];
}

/**
 * Request body for POST /runs/now — fire one attended agent turn immediately
 * through the headless harness (the "Run now" button). Distinct surface from
 * schedules: it does not create a saved schedule, it just executes once.
 */
export interface RunNowRequest {
  prompt: string;
  title?: string | null;
  /**
   * Target Agent for the run (`agentId == assistantId`; the backend field is
   * `ragAssistantId`). When set, the Agent's bound tools govern the turn and
   * `enabledTools` is left null.
   */
  ragAssistantId?: string | null;
  enabledTools?: string[] | null;
}

/**
 * `202` body of POST /runs/now. The turn runs in the background: poll
 * GET /runs/{runId} for its outcome. The answer lands as session `sessionId`.
 */
export interface RunStartedResponse {
  runId: string;
  sessionId: string;
  status: 'started';
}

/** Statuses a run ends in (`RunStatus` on the backend). */
export const TERMINAL_RUN_STATUSES = [
  'completed',
  'error',
  'timeout',
  'oauth_required',
  'client_tool_call',
  'needs_user',
] as const;

/** Response from GET /runs/{runId}: `started` until the run ends. */
export interface RunStatusResponse {
  runId: string;
  sessionId: string;
  status: 'started' | (typeof TERMINAL_RUN_STATUSES)[number];
  title?: string | null;
  error?: string | null;
  startedAt: string;
  finishedAt?: string | null;
}
