/**
 * One of an MCP server's tools, selectable on its own through a scoped
 * `serverId::toolName` ref (see `tool-selection.ts`).
 */
export interface ToolSelectorChild {
  /** The MCP tool name; also the scoped ref's suffix. */
  name: string;
  /** First paragraph of the docstring, shown under the name. */
  summary?: string;
  /** The rest of the docstring (`Args:` and so on), behind "Show details". */
  detail?: string;
}

/**
 * One selectable row. The parent maps whatever its source carries (a `Tool`, a
 * `BindableItem`) into this shape and keeps ownership of the data and the save:
 * the selector only renders rows and reports the new selection.
 */
export interface ToolSelectorItem {
  /** The ref written to the selection: a catalog tool id, a skill id. */
  id: string;
  name: string;
  description?: string;
  /** Catalog category. Rows group under it, and it becomes a filter chip. */
  group?: string;
  /** A short tag beside the name, such as `retiring`. Always visible text. */
  badge?: string;
  /**
   * A line under the name the user needs to read, such as why the row can't be
   * added. Visible text, never a tooltip: hover-only text is dead on touch.
   */
  note?: string;
  /** `warning` for something to act on; `muted` for provenance. */
  noteTone?: 'warning' | 'muted';
  /** Can't be toggled in either direction, and stays as it is. */
  locked?: boolean;
  /** Can be turned off but not on, such as a tool being retired. */
  addBlocked?: boolean;
  /** An MCP server's tools. When present, a selected row can be narrowed to some of them. */
  children?: readonly ToolSelectorChild[];
}
