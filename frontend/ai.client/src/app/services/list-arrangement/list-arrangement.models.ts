/**
 * How a user arranges a list page (docs/specs/agent-project-organization.md §5).
 * Mirrors `Arrangement` / `ArrangementUpdate` in `apis/shared/arrangements/models.py`.
 */

export type ArrangementSurface = 'agents' | 'projects';

export type ArrangementSort = 'recent' | 'name' | 'custom';

export interface Arrangement {
  sort: ArrangementSort;
  /** Custom order, first is top. Used from PR-3. */
  order: string[];
  /** Ids the user hid from the page. An id here may also be in `order`, keeping its place. */
  hidden: string[];
  updatedAt?: string | null;
}

/** What the page shows before, or without, a stored arrangement: today's order, nothing hidden. */
export const DEFAULT_ARRANGEMENT: Readonly<Arrangement> = Object.freeze({
  sort: 'recent',
  order: [],
  hidden: [],
  updatedAt: null,
});

/** The backend refuses more ids than this per list (`MAX_ARRANGEMENT_IDS`). */
export const MAX_ARRANGEMENT_IDS = 500;

/** A page's items split by the user's arrangement. */
export interface ArrangedList<T> {
  /** Items the user has not hidden, in the page's own order. */
  visible: T[];
  /** Items the user hid, in the page's own order. */
  hidden: T[];
}

/**
 * Split `items` by the arrangement's hidden ids.
 *
 * One pure function so the Agents and Projects pages cannot drift. Ids in the arrangement
 * that match no item (spec D3: the user lost access, or the item is gone) are ignored here
 * and kept in storage by {@link ListArrangementService}.
 */
export function arrangeList<T>(items: readonly T[], idOf: (item: T) => string, hiddenIds: ReadonlySet<string>): ArrangedList<T> {
  const visible: T[] = [];
  const hidden: T[] = [];
  for (const item of items) {
    (hiddenIds.has(idOf(item)) ? hidden : visible).push(item);
  }
  return { visible, hidden };
}

/**
 * The hidden list with `id` added or removed, keeping every other id, including ones that
 * match nothing on the page. Over the backend's cap, drops the oldest ids that are not on
 * the page (`currentIds`), which are the only ones that can go.
 */
export function withHidden(
  hidden: readonly string[],
  id: string,
  hide: boolean,
  currentIds: ReadonlySet<string>,
): string[] {
  const rest = hidden.filter((existing) => existing !== id);
  const next = hide ? [...rest, id] : rest;
  if (next.length <= MAX_ARRANGEMENT_IDS) return next;
  const overflow = next.length - MAX_ARRANGEMENT_IDS;
  const droppable = new Set(next.filter((existing) => !currentIds.has(existing)).slice(0, overflow));
  return next.filter((existing) => !droppable.has(existing));
}
