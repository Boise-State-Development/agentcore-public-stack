/**
 * Feature announcements — the client mirror of
 * `apis/shared/announcements/models.py` (`UserAnnouncement`).
 *
 * Deliberately narrower than the admin model: the server never sends
 * `targetRoles`, `state`, or `createdBy` to a user, so there is nothing here
 * to accidentally render.
 *
 * See `docs/specs/feature-announcements.md`.
 */

export type AnnouncementSurface = 'panel' | 'banner' | 'modal';
export type AnnouncementSeverity = 'info' | 'success' | 'warning';

/** What the user records about an announcement. Monotonic server-side (§D2). */
export type AnnouncementAckAction = 'seen' | 'dismissed' | 'acknowledged';

export interface Announcement {
  announcement_id: string;
  title: string;
  body_markdown: string;
  summary?: string | null;
  surfaces: AnnouncementSurface[];
  severity: AnnouncementSeverity;
  publish_at: string;
  expires_at?: string | null;
  requires_ack: boolean;
  cta_label?: string | null;
  cta_url?: string | null;
  revision: number;
  /** No acknowledgement at this revision — drives the unread dot. */
  is_unread: boolean;
  /** Acked an earlier revision, so the panel says "Updated" not "New" (§D4). */
  is_updated: boolean;
}

/**
 * `GET /announcements` — already filtered and capped by the server (§D5/§D7).
 *
 * `banner` is rendered by `components/announcement-banner`; `modal` by
 * `components/announcement-modal`, gated by `AnnouncementModalService`.
 */
export interface AnnouncementFeed {
  panel: Announcement[];
  banner: Announcement | null;
  modal: Announcement | null;
  unread_count: number;
}

export interface AnnouncementAckRequest {
  action: AnnouncementAckAction;
  surface: AnnouncementSurface;
}
