/**
 * Memory Spaces — user-owned, shareable markdown "second brains" (F5, A5 SPA surface).
 *
 * These interfaces mirror the app-api response models under
 * `apis/app_api/memory_spaces/` (which serialize camelCase). Keep them in
 * lockstep with that backend contract — a breaking change to either side must
 * update both in the same PR.
 */

/** Full role set on a space. `owner` is implicit (stored on the space). */
export type MemoryRole = 'owner' | 'editor' | 'viewer';

/** The two grantable roles (a share is never `owner`). */
export type ShareRole = 'viewer' | 'editor';

/** Entry kinds: mutable entity, append-only episodic, flat fact. */
export type EntryType = 'entity' | 'episodic' | 'fact';

/** A template a space can be seeded from (Blank / Chief of Staff / Research Notebook). */
export interface SpaceTemplate {
  templateId: string;
  name: string;
  description: string;
}

/** A space as it appears in the list (no index/entries body). */
export interface MemorySpaceSummary {
  spaceId: string;
  name: string;
  template: string;
  role: MemoryRole;
  ownerId: string;
  createdAt: string;
  updatedAt: string;
}

/** GET /memory/spaces */
export interface SpacesListResponse {
  spaces: MemorySpaceSummary[];
  templates: SpaceTemplate[];
}

/** A manifest entry ref (the catalog row; the body is fetched on demand). */
export interface MemoryEntryRef {
  slug: string;
  type: EntryType;
  description: string;
  size: number;
  updated: string;
  updatedBy: string;
  indexed: Record<string, unknown>;
}

/** GET /memory/spaces/{id} — summary + the MEMORY.md index text + entry refs. */
export interface MemorySpaceDetail extends MemorySpaceSummary {
  index: string;
  entries: MemoryEntryRef[];
}

/** GET /memory/spaces/{id}/entries/{slug} */
export interface EntryContent {
  slug: string;
  content: string;
}

/** One shared grant on a space. */
export interface SpaceMember {
  email: string;
  permission: ShareRole;
  createdAt: string;
}

/** GET /memory/spaces/{id}/shares */
export interface MembersListResponse {
  members: SpaceMember[];
}

// ---- requests ----------------------------------------------------------

export interface CreateSpaceRequest {
  name: string;
  template: string;
}

export interface UpsertEntryRequest {
  body: string;
  type?: EntryType;
  description?: string;
  indexed?: Record<string, unknown>;
}

export interface ShareRequest {
  email: string;
  permission: ShareRole;
}
