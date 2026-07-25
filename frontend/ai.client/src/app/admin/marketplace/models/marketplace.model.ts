/**
 * Agent Marketplace admin types (Phases 1–2, 5–6).
 *
 * Mirrors the backend wire models in `apis/shared/assistants/models.py`. Five of D10's
 * seven surfaces are covered — Review queue, Listings, Categories, Store front and
 * Default pins — plus the publisher profiles they read. The reports queue arrives with
 * Phase 8.
 */

import {
  AdminEdit,
  AdminStoreFrontResponse,
  AgentListing,
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

/**
 * The store-front row is the *same* shelf shape Discover renders, deliberately: an admin
 * curating the featured row should be looking at exactly the tile a user will see.
 */
export type { AdminStoreFrontResponse, AgentListing };

/** The featured row holds at most this many agents — see `storefront.MAX_FEATURED`. */
export const MAX_FEATURED = 10;

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

// ── default pins by role (D9, Phase 6) ─────────────────────────────────────────────

/** A role seeds at most this many agents — see `role_pins.MAX_ROLE_PINS`. */
export const MAX_ROLE_PINS = 25;

/** One capability the role does not grant, named rather than counted (D9.5). */
export interface MissingCapability {
  label: string;
  kind: string;
  /** Only an explicitly optional binding degrades to `limits` rather than blocking. */
  optional: boolean;
}

/**
 * One seeded agent as the admin console sees it: the shelf row plus the two checks that
 * decide whether the seed will do anything for this role's members.
 *
 * `reachable` is about *visibility* — a PRIVATE agent resolves to nothing for everyone
 * but its owner, because the user-side read access-checks every row. `state` is about
 * *capability* — the agent's model and bindings diffed against this role's granted
 * permissions. They fail for different reasons and have different people to fix them.
 */
export interface RoleAgentPinRow {
  agentId: string;
  name: string;
  tagline?: string;
  emoji?: string;
  iconUrl?: string;
  publisher?: { label: string; kind: PublisherKind; verified: boolean } | null;
  category: string;
  order: number;
  /** A locked seed cannot be dismissed by a member (D9.4). */
  locked: boolean;
  listingState?: ListingState | null;
  reachable: boolean;
  visibility: string;
  state: 'ready' | 'limits' | 'blocked';
  missing: MissingCapability[];
  /** What the role-level check could not decide — a memory space resolves per person. */
  notes: string[];
}

export interface RoleAgentPinsResponse {
  roleId: string;
  roleLabel: string;
  /**
   * ⚠️ D9.6 — `default` is consulted only for users who matched *zero* AppRoles and is
   * never merged alongside a matched role. Pins seeded there reach nobody who holds any
   * other role, which is why the console labels the chip rather than letting an admin
   * assume it means "everyone".
   */
  fallbackOnly: boolean;
  /** The role has no JWT mappings, so no user matches it at all. */
  unmapped: boolean;
  pins: RoleAgentPinRow[];
  /** Seeded ids whose agent no longer exists — reported, never pruned on read. */
  unavailable: string[];
}

/** Replace a role's seed list, in order. `order` belongs to the list, not to a row. */
export interface RoleAgentPinsUpdateRequest {
  pins: { agentId: string; locked: boolean }[];
}

