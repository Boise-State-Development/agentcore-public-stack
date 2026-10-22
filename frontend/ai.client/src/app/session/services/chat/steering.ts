import type { ContentBlock, Message } from '../models/message.model';

/**
 * Mid-turn steering: shared shapes and the wire-format helpers both the live
 * stream path and the reload path need. See docs/specs/mid-turn-steering.md.
 *
 * The backend frames an injected follow-up in tags so the model reads it as
 * the user speaking during the turn rather than as tool output. Those tags are
 * for the model, never for the reader — so every surface that renders a
 * restored message has to unwrap them, and both the live and reload paths go
 * through {@link unwrapSteeringText} to do it the same way.
 */
export const STEER_OPEN_TAG = '<user_message_during_turn>';
export const STEER_CLOSE_TAG = '</user_message_during_turn>';

/**
 * The user's words inside a steering block, or `null` if this is not one.
 *
 * Deliberately strict about the wrapper: a user who happens to type the tag
 * themselves in an ordinary message would otherwise have it eaten. Only a
 * block that is *entirely* one wrapper unwraps.
 */
export function unwrapSteeringText(text: string | null | undefined): string | null {
  if (!text) return null;
  const trimmed = text.trim();
  if (!trimmed.startsWith(STEER_OPEN_TAG) || !trimmed.endsWith(STEER_CLOSE_TAG)) {
    return null;
  }
  return trimmed.slice(STEER_OPEN_TAG.length, trimmed.length - STEER_CLOSE_TAG.length).trim();
}

/**
 * Build the user message a steering injection renders as.
 *
 * Its id is deliberately outside the `msg-{sessionId}-{index}` namespace: the
 * live message index counts client-visible messages, and a steer is not one of
 * them server-side (it rides *inside* the tool-result message). Minting from
 * the entry id keeps it unique, stable across re-renders, and impossible to
 * collide with an id the server will compute on reload.
 */
export function buildSteeringMessage(
  sessionId: string,
  entryId: string,
  text: string,
): Message {
  return {
    id: `steer-${sessionId}-${entryId}`,
    role: 'user',
    content: [{ type: 'text', text }],
    createdAt: new Date().toISOString(),
    steering: true,
  };
}

/**
 * Normalize restored history so a steered turn reads the way it did live.
 *
 * A steering injection is persisted *inside* the tool-result message, so a
 * reload hands us a user message whose content is `[toolResult…, text]`. Left
 * alone that renders as a user bubble full of raw wrapper tags, and — because
 * turn grouping breaks on every user message — splits the response it
 * interrupted into two groups. Both are fixed here rather than in the
 * renderer, so every consumer of the message list sees the same shape the live
 * stream produced.
 *
 * Tool-result blocks are dropped from the returned message because they have
 * already been folded into their `toolUse` block by
 * `matchToolResultsToToolUses`; keeping them would be a second copy that
 * nothing renders. Messages that carry no steering block are returned
 * untouched (identity), so ordinary histories pay only a scan.
 */
export function normalizeSteeringMessages(messages: Message[]): Message[] {
  let changed = false;
  const normalized = messages.map((message) => {
    if (message.role !== 'user') return message;

    let steeringText: string | null = null;
    const kept: ContentBlock[] = [];
    for (const block of message.content) {
      if (block.type === 'toolResult') continue;
      const unwrapped = block.type === 'text' ? unwrapSteeringText(block.text) : null;
      if (unwrapped !== null) {
        steeringText = steeringText === null ? unwrapped : `${steeringText}\n\n${unwrapped}`;
        continue;
      }
      kept.push(block);
    }

    if (steeringText === null) return message;

    changed = true;
    return {
      ...message,
      content: [...kept, { type: 'text', text: steeringText } as ContentBlock],
      steering: true,
    };
  });

  return changed ? normalized : messages;
}
