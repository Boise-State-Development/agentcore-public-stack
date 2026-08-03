import { ListingReachability } from './reachability';
/**
 * Agent Marketplace store types (Phase 2).
 *
 * Mirrors `AgentListingResponse` / `AgentStoreFrontResponse` on the backend. Note what is
 * *absent*: no `instructions`, no bindings, no owner. A shelf row is icon, name and one
 * line (D4), and the narrow shape is the point — this is the payload every browsing user
 * receives.
 */

/** How the store renders a publisher: a name, a kind, and the verified mark. */
export interface ListingPublisher {
  label: string;
  kind: 'institution' | 'department' | 'individual';
  verified: boolean;
}

/**
 * Publication state of an agent's listing (D2). Absent listing = never submitted.
 *
 * This vocabulary lives here rather than in the admin feature because both halves of
 * the flow render it: the reviewer's queue and the author's own card. A second copy
 * would be the one that eventually says "In Review" while the other says "In review".
 */
export type ListingState =
  | 'private'
  | 'in_review'
  | 'published'
  | 'changes_requested'
  | 'taken_down'
  /**
   * The author has asked to pull a live listing and an admin has not yet decided (§5.1).
   *
   * ⚠️ Still **live in the store**. Dropping it off the shelf the moment the author asked
   * would hand them the unilateral delisting this state exists to prevent, so it reads as a
   * pending request rather than as a removal.
   */
  | 'withdrawal_requested';

/** One admin edit to a listing's presentation, surfaced back to the author (D13). */
export interface AdminEdit {
  /** Author-facing field name — the backend already maps `icon_key` → "icon". */
  field: string;
  at: string;
  by: string;
}

/** Display metadata for each listing state, used by the badge components. */
export const LISTING_STATE_LABELS: Record<ListingState, string> = {
  private: 'Private',
  in_review: 'In review',
  published: 'Published',
  changes_requested: 'Changes requested',
  taken_down: 'Taken down',
  withdrawal_requested: 'Withdrawal requested',
};

/**
 * Badge classes per state. Mirrors the mockup's palette: published reads as success,
 * in-review as pending, and both "needs attention" states as stop.
 */
export const LISTING_STATE_CLASSES: Record<ListingState, string> = {
  private: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  in_review: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  published: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  changes_requested: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  taken_down: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  // Amber like in_review, not rose: this is work waiting on an admin, not a problem with
  // the listing — and it is still published while it waits.
  withdrawal_requested: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
};

/**
 * The marketplace publication state carried on an Agent (D2).
 *
 * Present on `GET /agents` as well as the detail read, so an author's own card can
 * render its state, the reviewer's note and the D13 edit trail without a second call.
 */
export interface AgentListingBlock {
  state: ListingState;
  category: string;
  /** Display-only attribution (D12) — never an access grant, and never rendered raw. */
  publisherId: string;
  submittedAt?: string;
  submittedBy?: string;
  reviewedAt?: string;
  reviewedBy?: string;
  /** The reviewer's reason. Renders inline on the author's card so they never have to ask. */
  reviewNote?: string;
  /** Append-only log of admin presentation edits (D13). */
  adminEdits?: AdminEdit[];
  /**
   * Which snapshot the store is serving, or absent when nothing is published.
   *
   * Read here as a fact about *now*: a listing back `in_review` with this set is an
   * update over something users can still see, and the UI has to say so — "In review"
   * alone reads as "not live", which is wrong and alarming for an author whose agent is.
   */
  publishedVersion?: number;
  /**
   * Where a pending update came from, and therefore where cancelling it puts it back.
   *
   * ⚠️ Meaningful only while `state === 'in_review'` — an earlier cycle can leave a value
   * behind, and the backend says so on the field. Every read here gates on the state.
   */
  submittedFrom?: ListingState;
}

/** Author submits an Agent for review (D2). */
export interface SubmitListingRequest {
  category: string;
  /** Omit to publish under the author's own individual profile (D12). */
  publisherId?: string;
  note?: string;
  /**
   * The author's explicit consent to make this agent PUBLIC as part of publishing it.
   * The marketplace is public-only and every agent starts PRIVATE, so this is the normal
   * path, not an edge case. Omitting it is refused by the backend — the widening is
   * consent, never a side effect of submitting.
   */
  makePublic?: boolean;
  /**
   * Shelf subtitle (D4). Omit to leave any existing tagline untouched — a resubmission
   * that does not touch the field must not blank it.
   */
  tagline?: string;
}

/** One skill that publication would make readable (D7.1). */
export interface SkillExposure {
  ref: string;
  label: string;
}

/**
 * The D7 answers, before the author commits.
 *
 * ⚠️ `blockReason` and `requiresPublic` are separate on purpose. A block is "leave this
 * dialog and go fix something" — the form is hidden, because nothing below it would help.
 * `requiresPublic` is "tick the box and submitting handles it". They were briefly one
 * field, and that made the *common* path a dead end: every agent starts PRIVATE, so a
 * first-time author was bounced to another screen to set visibility and come back.
 */
export interface ListingPreflight {
  agentId: string;
  exposedSkills: SkillExposure[];
  blockReason?: string | null;
  /**
   * This agent is not PUBLIC yet, so submitting must widen it. Drives the consent
   * checkbox — never disables Submit on its own.
   */
  requiresPublic?: boolean;
  /**
   * Who would be able to open this agent once shelved. At submit time this is now a
   * consequence of `requiresPublic`; it still matters to the reviewer, because an agent
   * published as PUBLIC can be narrowed afterwards.
   */
  reachability: ListingReachability;
}

/** The listing after a submission, plus the disclosures the author was shown (D7). */
export interface ListingSubmissionResponse {
  agentId: string;
  listing: AgentListingBlock;
  exposedSkills: SkillExposure[];
}

/** One row on a shelf. */
export interface AgentListing {
  agentId: string;
  name: string;
  tagline?: string;
  emoji?: string;
  /** Absent until icons ship (Phase 4); the SPA renders the generated fallback. */
  iconUrl?: string;
  publisher?: ListingPublisher | null;
  category: string;
}

/**
 * The result of setting or clearing an agent's square icon (D5).
 *
 * Both fields are absent after a remove, which is the signal to fall back to the
 * generated gradient without re-reading the agent.
 */
export interface AgentIconResponse {
  agentId: string;
  iconKey?: string;
  iconUrl?: string;
}

/** An admin-managed store category. `id` is immutable; `label` is what renders. */
export interface AgentCategory {
  id: string;
  label: string;
  order: number;
  enabled: boolean;
}

export interface AgentStoreResponse {
  listings: AgentListing[];
  /** Opaque cursor; only returned when browsing a single category. */
  nextCursor?: string | null;
}

export interface AgentStoreFrontResponse {
  /** The admin's curated row — the store's only ranking lever (D10). */
  featured: AgentListing[];
  categories: AgentCategory[];
}

/**
 * One row on the Pinned shelf (D8, D9).
 *
 * The shelf projection plus the two fields that say *why* it is pinned. `source` is
 * `'user'` whenever the viewer has their own pin — even when a role also seeds the same
 * agent, because that own pin is what survives the role pin being removed. `locked`
 * follows the *role's* seed regardless of source: a role that locks an agent has said its
 * members keep it.
 */
export interface PinnedAgent extends AgentListing {
  source: 'user' | 'role';
  /** A locked role pin cannot be removed; the control is hidden rather than disabled. */
  locked: boolean;
  pinnedAt?: string;
}

export interface AgentPinsResponse {
  pins: PinnedAgent[];
}

/** The featured row as the admin console sees it (D10). */
export interface AdminStoreFrontResponse {
  featured: AgentListing[];
  /**
   * Configured ids that no longer resolve as published listings. Reported rather than
   * pruned on read, so an admin can see why the row is short instead of watching a
   * curation silently rewrite itself.
   */
  unavailable: string[];
}

/** A category plus the listings currently on its shelf, for the Discover sections. */
export interface CategoryShelf {
  category: AgentCategory;
  listings: AgentListing[];
}

// ── problem reports, the reporter's half (D15, Phase 8) ────────────────────────────
/**
 * Why an agent is being reported.
 *
 * A small fixed set so the admin queue can sort by severity without reading every note.
 * `inappropriate` is the one meant to page a human rather than wait for a sweep, and
 * `suggestion` — which is not a defect at all — is deliberately last in that order.
 *
 * `suggestion` exists because feedback moved out of the store's detail page and into the
 * foot of the conversation, where "it should also do X" is as common as "it is broken".
 * It stays the same record in the same queue: a user does not know which kind of thing
 * they hit, and a second intake form would only mis-sort the ones who guess wrong.
 */
export type ReportReason = 'inaccurate' | 'broken' | 'inappropriate' | 'suggestion' | 'other';

export interface SubmitReportRequest {
  reason: ReportReason;
  note?: string;
  /**
   * The conversation the user opted to attach, for the curator's context.
   *
   * ⚠️ Opt-in and **server-verified**: the backend keeps it only if it is a session this
   * caller owns, and silently drops it otherwise rather than failing the submission.
   */
  sessionId?: string;
}

/**
 * What the reporter is told back — deliberately thin.
 *
 * ⚠️ A report is a *private message to the curator*. It is never rendered to another
 * browsing user and never feeds `usageCount`, the store front, or any ordering, so there
 * is no count, no queue position and no admin field here to leak one.
 */
export interface SubmitReportResponse {
  agentId: string;
  reason: ReportReason;
  state: 'open' | 'resolved' | 'dismissed';
  createdAt: string;
  /**
   * True when this updated a report the user already had open (D15.4) rather than adding
   * one — so the confirmation says "we updated your report" instead of implying a second
   * one is now queued.
   */
  replacedExisting: boolean;
}

/** The shelf subtitle's hard limit — mirrors `SubmitListingRequest.tagline` (D4). */
export const TAGLINE_MAX = 80;

/**
 * A starting tagline derived from an Agent's description (D4 backfill, #749).
 *
 * `tagline` postdates every Agent that existed before the Marketplace, and the field is
 * author-owned but was never author-settable — no Designer control wrote it. So a legacy
 * Agent arrives at submission with nothing for the shelf, and the store falls back to a
 * truncated `description`, which is exactly the mid-clause row D4 added `tagline` to avoid.
 *
 * Rather than requiring the author to invent one, submission prefills from the first
 * clause of the description and lets them edit it. Deriving is not the point — *showing*
 * it at the one moment the author is looking at what the shelf will say is. A bad
 * derivation is then one edit away instead of a surprise after publication.
 *
 * "First clause" is the first sentence or clause boundary, whichever comes first, trimmed
 * of its punctuation. Falls back to a hard truncation only when the text has no boundary
 * inside the limit — the case a human should be looking at anyway.
 */
export function deriveTagline(description: string | undefined | null): string {
  const text = (description ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  if (text.length <= TAGLINE_MAX) return text.replace(/[.;,\s]+$/, '');

  // Prefer a real boundary inside the limit; `.` `;` and `—` all end a clause.
  const window = text.slice(0, TAGLINE_MAX + 1);
  const boundary = Math.max(
    window.lastIndexOf('. '),
    window.lastIndexOf('; '),
    window.lastIndexOf(' — '),
  );
  if (boundary > 20) return window.slice(0, boundary).replace(/[.;,\s]+$/, '');

  // No boundary worth using — break on the last whole word instead of mid-token.
  const space = window.lastIndexOf(' ');
  const cut = space > 20 ? window.slice(0, space) : text.slice(0, TAGLINE_MAX);
  return cut.replace(/[.;,\s]+$/, '');
}
