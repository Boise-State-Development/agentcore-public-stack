/**
 * How a user arranges a list page (docs/specs/agent-project-organization.md §5).
 * Mirrors `Arrangement` / `ArrangementUpdate` in `apis/shared/arrangements/models.py`.
 */

export type ArrangementSurface = 'agents' | 'projects';

export type ArrangementSort = 'recent' | 'name' | 'custom';

export interface Arrangement {
  sort: ArrangementSort;
  /** Custom order, first is top. Read only while `sort` is `custom`. */
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

/** A page's items, sorted and split by the user's arrangement. */
export interface ArrangedList<T> {
  /** Items the user has not hidden, in the arrangement's order. */
  visible: T[];
  /** Items the user hid, in the arrangement's order. */
  hidden: T[];
  /**
   * Items to badge **New** (spec D6): under a saved custom order, the ones in neither the
   * order nor the hidden list, which arrived since the user last arranged.
   */
  newIds: ReadonlySet<string>;
}

export interface ArrangeOptions<T> {
  idOf: (item: T) => string;
  nameOf: (item: T) => string;
  arrangement: Arrangement;
}

/**
 * Sort and split `items` by the user's arrangement (spec §6.1).
 *
 * - `recent` keeps the page's own order (most recent first), today's behaviour.
 * - `name` sorts by name, ignoring case and accents.
 * - `custom` puts new items first in the page's order, then the saved order.
 *
 * One pure function so the Agents and Projects pages cannot drift. Ids in the arrangement
 * that match no item (spec D3: the user lost access, or the item is gone) are ignored here
 * and kept in storage by {@link ListArrangementService}.
 */
export function arrangeList<T>(items: readonly T[], options: ArrangeOptions<T>): ArrangedList<T> {
  const { idOf, nameOf, arrangement } = options;
  const hiddenIds = new Set(arrangement.hidden);
  const position = new Map(arrangement.order.map((id, index) => [id, index]));
  const newIds = new Set<string>();

  let sorted: T[];
  if (arrangement.sort === 'name') {
    sorted = [...items].sort((a, b) => nameOf(a).localeCompare(nameOf(b), undefined, { sensitivity: 'base' }));
  } else if (arrangement.sort === 'custom') {
    const fresh = items.filter((item) => !position.has(idOf(item)) && !hiddenIds.has(idOf(item)));
    const placed = items
      .filter((item) => position.has(idOf(item)))
      .sort((a, b) => position.get(idOf(a))! - position.get(idOf(b))!);
    const unplacedHidden = items.filter((item) => !position.has(idOf(item)) && hiddenIds.has(idOf(item)));
    sorted = [...fresh, ...placed, ...unplacedHidden];
    // Before the first save everything is "new", so nothing is badged until there is an order.
    if (arrangement.updatedAt) fresh.forEach((item) => newIds.add(idOf(item)));
  } else {
    sorted = [...items];
  }

  const visible: T[] = [];
  const hidden: T[] = [];
  for (const item of sorted) {
    (hiddenIds.has(idOf(item)) ? hidden : visible).push(item);
  }
  return { visible, hidden, newIds };
}

/**
 * Under a custom order, the arrangement with the page's new items folded into the order
 * where they show (the top), which is what clears their New badge (spec D6). Any other
 * sort is returned as it is. `pageIds` is every id the page lists, in the page's order.
 */
export function foldNew(arrangement: Arrangement, pageIds: readonly string[]): Arrangement {
  if (arrangement.sort !== 'custom') return arrangement;
  const placed = new Set([...arrangement.order, ...arrangement.hidden]);
  const fresh = pageIds.filter((id) => !placed.has(id));
  if (fresh.length === 0) return arrangement;
  return { ...arrangement, order: capIds([...fresh, ...arrangement.order], new Set(pageIds), 'end') };
}

/**
 * The arrangement after an Arrange → Done: `arrangedIds` top first and `hiddenIds` hidden,
 * keeping the stored ids the page does not show (spec D3) after them.
 */
export function withArranged(
  arrangement: Arrangement,
  arrangedIds: readonly string[],
  hiddenIds: ReadonlySet<string>,
  pageIds: readonly string[],
): Arrangement {
  const onPage = new Set(pageIds);
  const order = capIds([...arrangedIds, ...arrangement.order.filter((id) => !onPage.has(id))], onPage, 'end');
  const hidden = capIds(
    [...arrangement.hidden.filter((id) => !onPage.has(id)), ...arrangedIds.filter((id) => hiddenIds.has(id))],
    onPage,
    'start',
  );
  return { ...arrangement, sort: 'custom', order, hidden };
}

/** Drop ids the page does not show until the list fits the backend's cap, from one end. */
function capIds(ids: string[], onPage: ReadonlySet<string>, from: 'start' | 'end'): string[] {
  if (ids.length <= MAX_ARRANGEMENT_IDS) return ids;
  const overflow = ids.length - MAX_ARRANGEMENT_IDS;
  const unseen = ids.filter((id) => !onPage.has(id));
  const droppable = new Set(from === 'start' ? unseen.slice(0, overflow) : unseen.slice(-overflow));
  return ids.filter((id) => !droppable.has(id));
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
