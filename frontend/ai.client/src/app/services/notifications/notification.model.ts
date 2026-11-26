/**
 * The in-app inbox (shared-projects §5, PR-1.7). Mirrors
 * `backend/src/apis/shared/notifications/models.py` and the `actorName` the
 * `/notifications` route adds on read.
 */

export type NotificationKind =
  | 'project_invited'
  | 'project_role_changed'
  | 'project_removed'
  | 'project_ownership_transferred'
  | 'project_archived'
  | 'project_restored'
  | 'project_member_left'
  | 'project_task_shared'
  | 'project_proposal_pending'
  | 'project_proposal_decided'
  | 'project_memory_maintenance'
  | 'project_schedule_paused'
  // A personal schedule the platform stopped (proactive-agent-gaps slice 0).
  | 'schedule_paused'
  // Quota requests and extensions (quota-requests spec §11.1).
  | 'quota_request_submitted'
  | 'quota_request_decided'
  | 'quota_request_info_requested'
  | 'quota_extension_granted'
  | 'quota_extension_revoked'
  // Handoffs (agent-handoffs-and-consults §4.3, PR-0.3b): a background conversation's state.
  | 'handoff_needs_input'
  | 'handoff_done'
  | 'handoff_stopped'
  // A quiet watcher found something new (quiet-watcher §3.3, §3.5): one per finding.
  | 'agent_item'
  // A scheduled run paused on the user (answer-from-inbox §3.3): one per pause.
  // `agent_needs_approval` when every prompt is a tool approval.
  | 'agent_needs_approval'
  | 'agent_needs_input';

/** The inbox kinds that wait on the user until answered or expired. */
export const NEEDS_USER_NOTIFICATION_KINDS: ReadonlySet<NotificationKind> = new Set([
  'agent_needs_approval',
  'agent_needs_input',
]);

/** One prompt a paused run waits on, as its inbox row lists it: ids and one display line. */
export interface NeedsUserPrompt {
  interruptId: string;
  kind: 'tool_approval' | 'user_question' | 'mcp_elicitation' | 'browser_login';
  toolName?: string;
  serverName?: string;
  headline?: string;
}

/** How a needs-user row was settled (backend `NotificationResolution`); absent while it waits. */
export interface NotificationResolution {
  state: 'applying' | 'answered' | 'answered_elsewhere' | 'expired' | 'retry';
  decision?: 'approved' | 'declined' | 'answered' | 'skipped' | null;
  outcome?: 'finished' | 'waiting_again' | 'failed' | null;
  at: string;
  resumeRunId?: string | null;
}

/** The inbox kinds a handoff writes. */
export const HANDOFF_NOTIFICATION_KINDS: ReadonlySet<NotificationKind> = new Set([
  'handoff_needs_input',
  'handoff_done',
  'handoff_stopped',
]);

export interface AppNotification {
  notificationId: string;
  recipientEmail: string;
  kind: NotificationKind;
  projectId?: string | null;
  projectName?: string | null;
  /** Who did it; null when unknown. */
  actorEmail?: string | null;
  /** Their display name; null when the directory has none. */
  actorName?: string | null;
  /**
   * `role` for invitations, role changes and a member leaving (the role they had);
   * `shareId`, `title` and an optional `note` for a shared task;
   * `proposalId`, `slug`, and for a decision `decision` and an optional `note`, for a memory proposal;
   * `runId`, `fileCount` and up to ten `slugs` for a maintenance run's proposals;
   * `scheduleId`, `label`, `reason` and `createdBy` (the creator's email) for a schedule that stopped;
   * for quota: `requestId`, `outcome`, `amount` / `requestedAmount` (decimal strings), `through`
   * (a date), `standing`, `fundingLabel`, `note`, and `reason` on a revoke;
   * for a handoff: `handoffId`, `sourceSessionId`, `targetSessionId`, `agentId`, `agentName`
   * and `sourceTitle` (never request or conversation text), plus `waitingKind` on a
   * `handoff_needs_input` entry, so a click opens where the answer is;
   * for a watcher's finding (`agent_item`, no actor or project): `scheduleId`, `runId`,
   * `sessionId`, `label` (the schedule's), `title` (the finding's short display line),
   * `urgency` (`now` | `today` | `fyi`) and `key` (the finding's stable identity);
   * for a paused run (`agent_needs_*`, no actor or project): `scheduleId`, `runId`,
   * `runFinishedAt`, `sessionId`, `label`, `expiresAt`, `answerable` and up to five `prompts`.
   */
  payload: {
    role?: string;
    shareId?: string;
    title?: string;
    note?: string;
    proposalId?: string;
    slug?: string;
    decision?: string;
    runId?: string;
    fileCount?: number;
    slugs?: string[];
    scheduleId?: string;
    label?: string;
    reason?: string;
    createdBy?: string;
    handoffId?: string;
    sourceSessionId?: string;
    targetSessionId?: string;
    agentId?: string;
    agentName?: string;
    sourceTitle?: string;
    waitingKind?: string;
    sessionId?: string;
    urgency?: 'now' | 'today' | 'fyi';
    key?: string;
    runFinishedAt?: string;
    expiresAt?: string;
    answerable?: boolean;
    prompts?: NeedsUserPrompt[];
  } & Record<string, unknown>;
  createdAt: string;
  readAt?: string | null;
  resolution?: NotificationResolution | null;
}

export interface NotificationsResponse {
  /** Newest first. */
  notifications: AppNotification[];
  unreadCount: number;
  nextCursor?: string | null;
}
