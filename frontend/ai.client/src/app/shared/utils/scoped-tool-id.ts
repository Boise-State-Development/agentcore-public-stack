/**
 * Scoped tool identifiers — referencing an individual tool within an MCP server.
 *
 * Mirrors the backend `apis.shared.tools.scoped_ids`. A bare catalog tool id
 * (e.g. `fetch_url_content`) means the whole server / all of its tools; a
 * scoped id `<toolId>::<mcpToolName>` references a single tool the server
 * exposes. Used by the admin skills picker (bound tool ids) and model settings
 * (user preferences) so both can carry a subset of a server's tools.
 */

/** Delimiter between a catalog tool id and an individual MCP tool name. */
export const SCOPE_DELIMITER = '::';

/** True if `toolId` references a single tool within a server. */
export function isScopedToolId(toolId: string): boolean {
  return toolId.includes(SCOPE_DELIMITER);
}

/** Build the scoped id for one tool of an MCP-server catalog tool. */
export function makeScopedToolId(catalogToolId: string, mcpToolName: string): string {
  return `${catalogToolId}${SCOPE_DELIMITER}${mcpToolName}`;
}

/**
 * Split a possibly-scoped id into its base catalog id and individual tool name.
 * A bare id returns `{ base, name: null }`. Only the first delimiter splits.
 */
export function parseScopedToolId(toolId: string): { base: string; name: string | null } {
  const idx = toolId.indexOf(SCOPE_DELIMITER);
  if (idx === -1) {
    return { base: toolId, name: null };
  }
  const base = toolId.slice(0, idx);
  const name = toolId.slice(idx + SCOPE_DELIMITER.length).trim();
  return { base, name: name || null };
}

/** The catalog tool id a (possibly-scoped) id refers to. */
export function baseToolId(toolId: string): string {
  return parseScopedToolId(toolId).base;
}
