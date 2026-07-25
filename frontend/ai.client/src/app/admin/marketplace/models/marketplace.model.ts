/**
 * Agent Marketplace admin types (Phase 1).
 *
 * Mirrors the backend wire models in `apis/shared/assistants/models.py`. Phase 1 covers
 * two of the six admin surfaces — Review queue and Listings — plus the publisher profiles
 * they read. Store front, categories and default pins arrive in later phases.
 */

import {
  AdminEdit,
  ListingState,
  LISTING_STATE_CLASSES,
  LISTING_STATE_LABELS,
} from '../../../agents/models/store.model';

/**
 * The listing-state vocabulary is shared with the author's own surface (My Agents) and
 * is defined once, in the agents feature. Re-exported here so this file stays the one
 * import the admin pages need — but never redefined, because a reviewer and an author
 * looking at the same listing must read the same word for it.
 */
export type { AdminEdit, ListingState };
export { LISTING_STATE_CLASSES, LISTING_STATE_LABELS };

export type PublisherKind = 'institution' | 'department' | 'individual';

/**
 * A display identity a listing is attributed to.
 *
 * Display only — never an access grant. `ownerName` on the row is who actually owns the
 * agent and whose skills resolve when it runs.
 */
export interface PublisherProfile {
  id: string;
  label: string;
  kind: PublisherKind;
  verified: boolean;
  iconKey?: string;
  order: number;
  enabled: boolean;
  createdAt?: string;
  updatedAt?: string;
}

/** A row in the Review queue or the Listings table. */
export interface AdminListingRow {
  agentId: string;
  name: string;
  tagline?: string;
  emoji?: string;
  iconKey?: string;
  /** Where to render the icon from (Phase 4); absent → the generated gradient. */
  iconUrl?: string;
  /** The author — who to talk to about behavior. Distinct from `publisher`. */
  ownerName: string;
  publisher?: PublisherProfile | null;
  category: string;
  state: ListingState;
  usageCount: number;
  submittedAt?: string;
  reviewedAt?: string;
  reviewNote?: string;
  updatedAt: string;
  adminEdits: AdminEdit[];
}

export interface AdminListingsResponse {
  listings: AdminListingRow[];
  /** Submissions awaiting review; badges the nav so the queue is visible, not discovered. */
  pendingCount: number;
}

export interface ReviewListingRequest {
  decision: 'approve' | 'request_changes';
  note?: string;
  category?: string;
  publisherId?: string;
}

export interface TakedownRequest {
  reason: string;
}

/**
 * Presentation-only patch. The backend refuses behavior fields (`instructions`,
 * `bindings`, `modelConfig`, `starters`, `visibility`) with a 422 — an admin editing
 * behavior would own something they did not write and cannot test.
 */
export interface ListingPatchRequest {
  name?: string;
  tagline?: string;
  iconKey?: string;
  category?: string;
  publisherId?: string;
}

/**
 * An admin-managed store category (D10).
 *
 * `id` is immutable — it is half of the directory partition key, so renaming a category
 * changes `label` only. That is why the form offers no id field after creation.
 */
export interface AgentCategory {
  id: string;
  label: string;
  order: number;
  enabled: boolean;
  createdAt?: string;
  updatedAt?: string;
}

