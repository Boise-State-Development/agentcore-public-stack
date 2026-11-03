/**
 * The review sheet's copy of the server's handoff wrapper
 * (`backend/src/apis/shared/handoff/context.py`).
 *
 * The sheet's "Preview full prompt" must show exactly what the Agent will receive.
 * For an unedited card the server already returned that (`prompt`). Once the user
 * edits the card or the request, the sheet rebuilds it here, the same way the
 * Runtime will on the first turn. `handoff-prompt.fixture.json` is generated from
 * the Python and checked by both sides' tests, so the two cannot drift silently.
 */

const HANDOFF_CONTEXT_TAG = 'handoff_context';
const EXCERPT_TAG = 'conversation_excerpt';
const CONTEXT_NOTE = 'The user brought this context from another of their conversations.';

/** A browser textarea can hand back `\r\n`; the server normalizes before wrapping and hashing. */
export function normalizeContextText(text: string): string {
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function neutralizeCloseTags(text: string): string {
  let out = text;
  for (const tag of [HANDOFF_CONTEXT_TAG, EXCERPT_TAG]) {
    out = out.split(`</${tag}`).join(`<\\/${tag}`);
  }
  return out;
}

function neutralizeInner(body: string): string {
  const close = `</${EXCERPT_TAG}>`;
  if (body.endsWith(close)) {
    return neutralizeCloseTags(body.slice(0, -close.length)) + close;
  }
  return neutralizeCloseTags(body);
}

/** Wrap a context card as the Agent receives it. An empty card wraps to `''`. */
export function wrapHandoffContext(text: string, sourceTitle: string): string {
  const body = normalizeContextText(text);
  if (!body) return '';
  const title = escapeAttr(sourceTitle.trim() || 'Untitled conversation');
  return (
    `<${HANDOFF_CONTEXT_TAG} from_conversation="${title}">\n` +
    `${CONTEXT_NOTE}\n` +
    `${neutralizeInner(body)}\n` +
    `</${HANDOFF_CONTEXT_TAG}>`
  );
}

/** The Agent's first message: the wrapped context, a blank line, then the request. */
export function buildHandoffMessage(contextBlock: string, request: string): string {
  const trimmed = request.trim();
  if (!contextBlock) return trimmed;
  if (!trimmed) return contextBlock;
  return `${contextBlock}\n\n${trimmed}`;
}

/**
 * The context a handoff's first message carried, for the "Context from" panel on its bubble.
 * `null` when the message has none (a live bubble, before reload, holds only what was typed).
 */
export function extractHandoffContext(text: string): string | null {
  const open = text.indexOf(`<${HANDOFF_CONTEXT_TAG} `);
  const close = text.indexOf(`</${HANDOFF_CONTEXT_TAG}>`);
  if (open !== 0 || close < 0) return null;
  const lines = text.slice(0, close).split('\n');
  // Drop the tag line and the constant note; what's left is the card as sent.
  return lines.slice(2).join('\n').trim() || null;
}
