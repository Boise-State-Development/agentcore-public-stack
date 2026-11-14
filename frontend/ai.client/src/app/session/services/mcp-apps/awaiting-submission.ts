/**
 * The `structuredContent.status` an MCP App write tool returns while its
 * form waits for the user (GitHub's `NewToolResultAwaitingFormSubmission`).
 *
 * Such a result is `status: 'error'` on purpose, so an agent does not chain
 * calls or claim the write happened. It has not failed. The server sends this
 * marker itself on MCP 2026-07-28, and it rides the result through the live
 * stream and the persisted message, so a reload agrees with what streamed
 * (`apis/shared/mcp_apps/awaiting_submission.py`).
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
