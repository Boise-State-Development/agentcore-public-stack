/**
 * Admin-side announcement types — the client mirror of
 * `apis/shared/announcements/models.py` (`AnnouncementResponse`).
 *
 * Wider than the user-facing model in
 * `services/announcements/announcement.model.ts`, which deliberately omits
 * `state`, `target_roles`, `show_to_new_users` and `created_by`. Keep them
 * separate: collapsing them into one type is how admin-only fields end up
 * being served to users.
 *
 * See `docs/specs/feature-announcements.md`.
 */

export type AnnouncementSurface = 'panel' | 'banner' | 'modal';
export type AnnouncementSeverity = 'info' | 'success' | 'warning';
export type AnnouncementState = 'draft' | 'scheduled' | 'published' | 'archived';

export interface Announcement {
  announcement_id: string;
  title: string;
  body_markdown: string;
  summary?: string | null;
  surfaces: AnnouncementSurface[];
  severity: AnnouncementSeverity;
  state: AnnouncementState;
  publish_at: string;
  expires_at?: string | null;
  /**
   * A **display filter, not an RBAC grant** (spec §D9). This list is written
   * only to the announcement; it is never mirrored into a role's `granted*`
   * arrays, and `apis/shared/rbac/` knows nothing about it. Visibility of a
   * notice is not access control.
   */
  target_roles: string[];
  show_to_new_users: boolean;
  requires_ack: boolean;
  cta_label?: string | null;
  cta_url?: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface AnnouncementListResponse {
  announcements: Announcement[];
  total: number;
}

/** POST body. The server always creates as a draft or scheduled — never live. */
export interface AnnouncementCreateRequest {
  title: string;
  body_markdown: string;
  summary?: string | null;
  surfaces: AnnouncementSurface[];
  severity: AnnouncementSeverity;
  state: 'draft' | 'scheduled';
  publish_at?: string | null;
  expires_at?: string | null;
  target_roles: string[];
  show_to_new_users: boolean;
  requires_ack: boolean;
  cta_label?: string | null;
  cta_url?: string | null;
}

/**
 * PATCH body. `state` and `revision` are absent by design — both are
 * transitions, owned by the publish / archive / revise endpoints, not by an
 * edit that looks like a body change.
 */
export type AnnouncementUpdateRequest = Partial<
  Omit<AnnouncementCreateRequest, 'state'>
>;
