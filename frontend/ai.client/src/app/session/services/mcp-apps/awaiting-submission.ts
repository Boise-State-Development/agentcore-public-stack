/**
 * The `structuredContent.status` an MCP App write tool returns while its
 * form waits for the user (GitHub's `NewToolResultAwaitingFormSubmission`).
 *
 * Such a result is `status: 'error'` on purpose, so an agent does not chain
 * calls or claim the write happened. It has not failed. The backend writes
 * this marker onto the result at intake even when the server sent the bare
 * stub (our MCP protocol version predates the marker), so the live stream and
 * a reloaded conversation both carry it (`apis/shared/mcp_apps/awaiting_submission.py`).
 */
export const AWAITING_USER_SUBMISSION = 'awaiting_user_submission';

/** True when a tool result is an MCP App form waiting for the user. */
export function isAwaitingUserSubmission(
  result: { structuredContent?: unknown } | null | undefined,
): boolean {
  const structured = result?.structuredContent;
  return (
    !!structured &&
    typeof structured === 'object' &&
    (structured as { status?: unknown }).status === AWAITING_USER_SUBMISSION
  );
}
