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
 * How far a published listing has drifted from what the reviewer approved (#744).
 *
 * Unlike `ListingState` this is admin-only vocabulary — it answers a curator's question
 * ("has this changed under me?"), not an author's, so it is defined here rather than in
 * the shared store model.
 *
 * **These are two different claims and are styled differently on purpose.**
 * `'instructions'` is measured: the instructions hash differs from the one recorded at
 * approval, so behavior definitely changed — that is the governance signal D2's "no
 * re-review on edit" non-goal leaves uncovered, and it is worth an admin's attention.
 * `'edited'` is inferred from timestamps on listings approved before the hash baseline
 * existed; the cause is unknown and is just as likely to be a rename or an admin's own
 * D13 presentation edit. Rendering the weak one as urgently as the strong one is how the
 * whole marker gets learned-ignored, so it stays visually quieter.
 */
export type ListingDrift = 'instructions' | 'edited';

export const LISTING_DRIFT_LABELS: Record<ListingDrift, string> = {
  instructions: 'Instructions changed',
  edited: 'Edited since review',
};

export const LISTING_DRIFT_CLASSES: Record<ListingDrift, string> = {
  instructions: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  edited: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

/** The hover explanation for each marker — the label alone does not say what to do. */
export const LISTING_DRIFT_TOOLTIPS: Record<ListingDrift, string> = {
  instructions:
    "This agent's instructions differ from the version that was approved. Its behavior " +
    'has changed since review, and any locked role pins cannot be opted out of.',
  edited:
    'This listing was edited after it was reviewed, but it predates change tracking, so ' +
    'we cannot tell whether its behavior changed. It may only be a rename or a ' +
    'presentation fix.',
};

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
  /**
   * Post-approval drift, derived server-side (#744). Only ever set on a published listing.
   *
   * The two values are different claims and must not render alike. `'instructions'` is
   * *measured* — the instructions hash differs from the one recorded at approval, so
   * behavior definitely changed. `'edited'` is *inferred* — the record was written after
   * the review, but the cause is unknown and may be an admin's own presentation edit.
   * Only listings approved before the hash baseline shipped can report `'edited'`.
   */
  drift?: ListingDrift;
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

/**
 * Warn past this many *locked* seeds on one role (#748).
 *
 * A threshold, not a limit, and the distinction is the whole decision. A locked seed
 * cannot be dismissed by the member who receives it, so an admin choosing between "seed"
 * and "seed locked" has no reason not to lock — locking guarantees the rollout lands and
 * the cost falls on someone else's sidebar. Left unchecked the dominant strategy is to
 * lock everything and Pinned stops being the user's shelf.
 *
 * A hard cap was considered and rejected: pins merge as a union across every role a user
 * matches and a lock from any one wins, so a per-role cap would not bound what any
 * individual sees, and the union itself is not cappable (membership resolves per user from
 * Entra claims, so it is unknowable at write time; enforcing at read time would silently
 * drop an admin's lock for some users). Friction has no such failure mode, and the number
 * below can move without breaking anyone's saved list.
 */
export const LOCK_WARN_THRESHOLD = 3;

/** One capability the role does not grant, named rather than counted (D9.5). */
export interface MissingCapability {
  label: string;
  kind: string;
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
  /** Two states, not three — see `RunnabilityState` in the agents feature (#747). */
  state: 'ready' | 'blocked';
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
  /**
   * Locked seeds held by *other* roles (#748).
   *
   * A member's shelf is the union across every role they match, and a lock from any one
   * of them wins — so this role's locked count is only part of what an individual gets.
   * Reported, never enforced: role membership resolves per user from Entra claims, so the
   * union is not knowable at write time, and capping it at read time would silently drop
   * an admin's lock for some users.
   */
  lockedElsewhere: number;
  /** How many other roles lock at least one seed — the spread behind `lockedElsewhere`. */
  lockedElsewhereRoles: number;
  pins: RoleAgentPinRow[];
  /** Seeded ids whose agent no longer exists — reported, never pruned on read. */
  unavailable: string[];
}

/** Replace a role's seed list, in order. `order` belongs to the list, not to a row. */
export interface RoleAgentPinsUpdateRequest {
  pins: { agentId: string; locked: boolean }[];
}


// ── problem reports (D15, Phase 8) ─────────────────────────────────────────────────
/**
 * Why an agent was reported. A fixed set so the queue sorts by severity without anyone
 * reading every note; `inappropriate` is the one that should page a human.
 */
export type ReportReason = 'inaccurate' | 'broken' | 'inappropriate' | 'other';

/**
 * A report's own tiny lifecycle. Deliberately **not** a mirror of `ListingState`: a
 * report is a note *about* an agent, not a state *of* it, and resolving one never changes
 * the listing (D15.5).
 */
export type ReportState = 'open' | 'resolved' | 'dismissed';

/**
 * One row in the admin Reports queue.
 *
 * ⚠️ `reporterId` / `reporterName` are **admin-only** (D15.2). Admins need identity to
 * spot a brigade or a grudge; the author needs the substance and never the name. Nothing
 * on this interface may be rendered on a user-facing surface.
 */
export interface AdminReportRow {
  reportId: string;
  agentId: string;
  agentName: string;
  emoji?: string;
  iconUrl?: string;
  ownerName?: string;
  /** Context only — resolving a report never writes this (D15.5). */
  listingState?: ListingState | null;
  /** The agent is gone; the row is shown so the admin can still clear it. */
  agentMissing: boolean;
  reporterId: string;
  reporterName: string;
  reason: ReportReason;
  note?: string;
  state: ReportState;
  createdAt: string;
  resolvedAt?: string;
  resolvedBy?: string;
  resolutionNote?: string;
}

export interface AdminReportsResponse {
  reports: AdminReportRow[];
  /** Awaiting triage — badges the admin nav alongside submissions (D10). */
  openCount: number;
}

/** Resolve or dismiss. The note is the admin's record, never forwarded to the author. */
export interface ResolveReportRequest {
  decision: 'resolve' | 'dismiss';
  note?: string;
}

/** The two integers D10 puts on the nav, fetched without loading either queue. */
export interface AdminQueueCounts {
  pendingCount: number;
  openReportCount: number;
}
