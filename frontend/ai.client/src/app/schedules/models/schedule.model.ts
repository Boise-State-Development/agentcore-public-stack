/**
 * Front-end models for scheduled agent runs (`/schedules/*`).
 *
 * Mirrors the backend contract from `apis/app_api/schedules/` — field names
 * are the camelCase aliases the backend serializes. Bounded cadence set
 * only (daily / weekday / weekly) — no cron UI, per
 * docs/specs/scheduled-agent-runs.md §3.
 */

export type ScheduleCadence = 'daily' | 'weekday' | 'weekly';
export type ScheduledPromptState = 'active' | 'paused' | 'paused_error';

/** Public view of a scheduled prompt (backend ScheduledPromptResponse). */
export interface ScheduledPrompt {
  scheduleId: string;
  assistantId?: string | null;
  label: string;
  promptText: string;
  cadence: ScheduleCadence;
  hourLocal: number;
  weekday?: number | null;
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
