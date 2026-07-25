/**
 * Agent Marketplace admin types (Phase 1).
 *
 * Mirrors the backend wire models in `apis/shared/assistants/models.py`. Phase 1 covers
 * two of the six admin surfaces — Review queue and Listings — plus the publisher profiles
 * they read. Store front, categories and default pins arrive in later phases.
 */

/** Publication state of an agent's listing. Absent listing = never submitted. */
export type ListingState =
  | 'private'
  | 'in_review'
  | 'published'
  | 'changes_requested'
  | 'taken_down';

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

/** One admin edit to a listing's presentation, surfaced back to the author. */
export interface AdminEdit {
  field: string;
  at: string;
  by: string;
}

/** A row in the Review queue or the Listings table. */
export interface AdminListingRow {
  agentId: string;
  name: string;
  tagline?: string;
  emoji?: string;
  iconKey?: string;
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

/** Display metadata for each listing state, used by the badge component. */
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
  private:
    'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  in_review:
    'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  published:
    'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  changes_requested:
    'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  taken_down:
    'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
};
