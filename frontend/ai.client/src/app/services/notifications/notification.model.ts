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
  | 'project_memory_maintenance';

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
   * `runId`, `fileCount` and up to ten `slugs` for a maintenance run's proposals.
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
  } & Record<string, unknown>;
  createdAt: string;
  readAt?: string | null;
}

export interface NotificationsResponse {
  /** Newest first. */
  notifications: AppNotification[];
  unreadCount: number;
  nextCursor?: string | null;
}
