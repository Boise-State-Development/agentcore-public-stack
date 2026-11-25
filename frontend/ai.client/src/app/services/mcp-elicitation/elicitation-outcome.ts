/**
 * What the user did with an MCP server's form, as the backend records it on
 * the tool result (`OUTCOME_KEY` in `apis/shared/mcp_elicitation/forms.py`).
 * Never the answer itself, which goes to the server only.
 *
 * It rides the live `tool_result` event and the persisted message, so a reload
 * agrees with what streamed (docs/specs/mcp-elicitation.md §13).
 */
export interface McpElicitationOutcome {
  action: 'accept' | 'decline' | 'cancel';
}

/**
 * True when the call ended because the user declined or dismissed the form.
 *
 * The server reports that as an error (GitHub's "Repository deletion was not
 * confirmed"), but it is the user's decision, so the rail does not count it
 * as a failure. An answer the server then refused (`accept` with an error)
 * still reads as a failure: only the server knows why it refused.
 */
export function isDeclinedElicitation(
  result: { elicitation?: unknown } | null | undefined,
): boolean {
  const outcome = result?.elicitation;
  if (!outcome || typeof outcome !== 'object') return false;
  const action = (outcome as { action?: unknown }).action;
  return action === 'decline' || action === 'cancel';
}

/** The outcome from an untyped result, or undefined when it has none. */
export function readElicitationOutcome(value: unknown): McpElicitationOutcome | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const action = (value as { action?: unknown }).action;
  return action === 'accept' || action === 'decline' || action === 'cancel'
    ? { action }
    : undefined;
}
