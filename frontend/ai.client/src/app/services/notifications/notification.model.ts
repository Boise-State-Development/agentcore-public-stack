/**
 * The in-app inbox (shared-projects §5, PR-1.7). Mirrors
 * `backend/src/apis/shared/notifications/models.py` and the `actorName` the
 * `/notifications` route adds on read.
 */

export type NotificationKind =
  | 'project_invited'
  | 'project_role_changed'
  | 'project_removed'
  | 'project_ownership_transferred';

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
  /** `role` for invitations and role changes. */
  payload: { role?: string } & Record<string, unknown>;
  createdAt: string;
  readAt?: string | null;
}

export interface NotificationsResponse {
  /** Newest first. */
  notifications: AppNotification[];
  unreadCount: number;
  nextCursor?: string | null;
}
