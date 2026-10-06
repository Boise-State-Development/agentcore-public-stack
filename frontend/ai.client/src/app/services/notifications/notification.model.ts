/**
 * The in-app inbox (shared-projects §5, PR-1.7). Mirrors
 * `backend/src/apis/shared/notifications/models.py`.
 */

export type NotificationKind =
  | 'project_invited'
  | 'project_role_changed'
  | 'project_removed'
  | 'project_ownership_transferred'
  | 'project_archived'
  | 'project_restored'
  | 'project_member_left';

export interface AppNotification {
  notificationId: string;
  recipientEmail: string;
  kind: NotificationKind;
  projectId?: string | null;
  projectName?: string | null;
  /** Who did it; null when unknown. */
  actorEmail?: string | null;
  /** `role` for invitations, role changes and a member leaving (the role they had). */
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
