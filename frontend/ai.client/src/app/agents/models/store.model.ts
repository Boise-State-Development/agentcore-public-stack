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
