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

/** The fields of a persisted App tool call this module reads. */
export interface SubmittedCall {
  toolName: string;
  isError: boolean;
}

/**
 * The call that completed a form this tool left waiting, or null.
 *
 * A write tool that returned `awaiting_user_submission` is finished by the
 * App itself: the user submits the form and the App calls the same tool
 * again from its frame (GitHub's `issue_write` with `_ui_submitted`). The
 * conversation keeps the original "waiting" result, so on reload the App
 * would be handed it again and redraw the empty form over an issue that
 * already exists. The App's own call is persisted as a card on this frame,
 * and its result is the one to hand back instead.
 *
 * Matches the latest successful card for the same tool, by bare name or
 * as the `<target>___<name>` a Gateway target gives the agent. `cards`
 * are oldest-first, as `McpAppCardStateService.cardsFor` returns them.
 */
export function findSubmittedCall<T extends SubmittedCall>(
  result: { structuredContent?: unknown } | null | undefined,
  toolName: string,
  cards: readonly T[],
): T | null {
  if (!toolName || !isAwaitingUserSubmission(result)) return null;
  for (let i = cards.length - 1; i >= 0; i--) {
    const card = cards[i];
    if (card.isError || !card.toolName) continue;
    if (card.toolName === toolName || toolName.endsWith(`___${card.toolName}`)) {
      return card;
    }
  }
  return null;
}
