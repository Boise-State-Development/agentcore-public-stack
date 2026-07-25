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
  | 'taken_down';

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
}

/** Author submits an Agent for review (D2). */
export interface SubmitListingRequest {
  category: string;
  /** Omit to publish under the author's own individual profile (D12). */
  publisherId?: string;
  note?: string;
}

/** One skill that publication would make readable (D7.1). */
export interface SkillExposure {
  ref: string;
  label: string;
}

/**
 * The D7 answers, before the author commits.
 *
 * `blockReason` non-null means submission is impossible (a `memory_space` binding,
 * D7.2) and the text explains why. The dialog renders it; it never re-derives it.
 */
export interface ListingPreflight {
  agentId: string;
  exposedSkills: SkillExposure[];
  blockReason?: string | null;
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
  /** Curated row; empty until the store-front admin ships in Phase 5. */
  featured: AgentListing[];
  categories: AgentCategory[];
}

/** A category plus the listings currently on its shelf, for the Discover sections. */
export interface CategoryShelf {
  category: AgentCategory;
  listings: AgentListing[];
}
