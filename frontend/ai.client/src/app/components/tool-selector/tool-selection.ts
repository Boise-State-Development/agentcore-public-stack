import { baseToolId, makeScopedToolId } from '../../shared/utils/scoped-tool-id';

/**
 * The selection rules behind `<app-tool-selector>`, kept pure so every surface
 * that picks tools shares one definition of what a selection *is*.
 *
 * A selection is a set of refs, held verbatim. A ref is either a bare catalog id
 * (the whole tool, or the whole MCP server) or a scoped `serverId::toolName` that
 * selects one of a server's tools. The invariant: a server with *every* tool
 * selected is stored as the bare ref, never as N scoped ones. That keeps an
 * untouched record byte-identical to what it had, and it is what the backend's
 * `collect_tool_name_filters` means by whole-server anyway.
 */

/** The item is selected, whole or narrowed to some of its tools. */
export function isItemSelected(selected: ReadonlySet<string>, id: string): boolean {
  for (const ref of selected) {
    if (baseToolId(ref) === id) return true;
  }
  return false;
}

/** The bare ref is present: every one of the server's tools. */
export function isWholeItemSelected(selected: ReadonlySet<string>, id: string): boolean {
  return selected.has(id);
}

/** Drop the item and every scoped ref under it. */
export function withoutItem(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set<string>();
  for (const ref of selected) {
    if (baseToolId(ref) !== id) next.add(ref);
  }
  return next;
}

/** Deselect a selected item entirely; select an unselected one whole. */
export function toggleItem(selected: ReadonlySet<string>, id: string): Set<string> {
  if (isItemSelected(selected, id)) return withoutItem(selected, id);
  const next = new Set(selected);
  next.add(id);
  return next;
}

/** One of a server's tools is on. The bare ref means all of them, this one included. */
export function isChildSelected(selected: ReadonlySet<string>, id: string, name: string): boolean {
  return selected.has(id) || selected.has(makeScopedToolId(id, name));
}

/** How many of a server's tools are on, for the row's `2 of 3 tools`. */
export function selectedChildCount(
  selected: ReadonlySet<string>,
  id: string,
  childNames: readonly string[],
): number {
  return childNames.filter((name) => isChildSelected(selected, id, name)).length;
}

/**
 * Turn one of a server's tools on or off and re-derive the server's refs: all on
 * collapses to the bare ref, none on deselects the server entirely (an empty scoped
 * set is not something the backend can store, and "selected with nothing selected"
 * is not a state worth a third rendering).
 */
export function toggleChild(
  selected: ReadonlySet<string>,
  id: string,
  childNames: readonly string[],
  name: string,
): Set<string> {
  const on = new Set(childNames.filter((n) => isChildSelected(selected, id, n)));
  if (on.has(name)) on.delete(name);
  else on.add(name);

  const next = withoutItem(selected, id);
  if (on.size === 0) return next;
  if (on.size === childNames.length) {
    next.add(id);
    return next;
  }
  for (const n of childNames) {
    if (on.has(n)) next.add(makeScopedToolId(id, n));
  }
  return next;
}
