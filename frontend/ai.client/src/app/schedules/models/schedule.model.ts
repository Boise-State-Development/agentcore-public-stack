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

/** Response from POST /runs/now. The run also lands as a session. */
export interface RunNowResponse {
  runId: string;
  sessionId: string;
  status: string;
  finalMessage: string;
  stopReason?: string | null;
  error?: string | null;
  title?: string | null;
}
