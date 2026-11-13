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

import { DeliveryArtifact, DeliveryBlock, DeliveryFile, FileLine } from './handoff.models';

const HANDOFF_CONTEXT_TAG = 'handoff_context';
const EXCERPT_TAG = 'conversation_excerpt';
const ARTIFACT_TAG = 'artifact';
const FILE_TAG = 'file';
const CONTEXT_NOTE = 'The user brought this context from another of their conversations.';

/** A browser textarea can hand back `\r\n`; the server normalizes before wrapping and hashing. */
export function normalizeContextText(text: string): string {
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Tags a body may neither open nor close (handoffs spec §9.6 rule 5), as in `context.py`. */
const FORGEABLE_TAGS = [HANDOFF_CONTEXT_TAG, 'conversation_message', 'user_message_during_turn', ARTIFACT_TAG, FILE_TAG];

function neutralizeCloseTags(text: string): string {
  // The excerpt opens with its own tag, so only its closing form is defused.
  let out = text.split(`</${EXCERPT_TAG}`).join(`<\\/${EXCERPT_TAG}`);
  for (const tag of FORGEABLE_TAGS) {
    out = out.split(`</${tag}`).join(`<\\/${tag}`).split(`<${tag}`).join(`<\\${tag}`);
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

/** A file's size as its line names it (`format_file_size`): integer rounding, so both sides agree. */
export function formatFileSize(sizeBytes: number): string {
  const size = Math.max(0, Math.trunc(sizeBytes));
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.floor((size + 512) / 1024)} KB`;
  const tenths = Math.floor((size * 10 + 524_288) / 1_048_576);
  return `${Math.floor(tenths / 10)}.${tenths % 10} MB`;
}

/**
 * The upload id a shared file is read under in the receiving conversation (`reference_upload_id`):
 * the original's id and the first 12 hex digits of the handoff or delivery that shared it.
 */
export function referenceUploadId(originalUploadId: string, linkId: string): string {
  return `${originalUploadId}_ref_${linkId.replace(/-/g, '').slice(0, 12)}`;
}

/** The `<file>` line for `file` in `receivingSessionId` (`line_for`): a file handed back names its original. */
export function fileLineFor(file: DeliveryFile, receivingSessionId: string, linkId: string): FileLine {
  return {
    uploadId: file.sessionId === receivingSessionId ? file.uploadId : referenceUploadId(file.uploadId, linkId),
    filename: file.filename,
    mimeType: file.mimeType,
    sizeBytes: file.sizeBytes,
  };
}

function renderFileLine(file: FileLine): string {
  return (
    `<${FILE_TAG} upload_id="${escapeAttr(file.uploadId)}" name="${escapeAttr(file.filename)}" ` +
    `type="${escapeAttr(file.mimeType)}" size="${formatFileSize(file.sizeBytes)}"/>`
  );
}

/**
 * Wrap a context card as the Agent receives it, with a `<file>` line per shared file (§4.9).
 * An empty card with no files wraps to `''`; a removed card with files still names them.
 */
export function wrapHandoffContext(text: string, sourceTitle: string, files: readonly FileLine[] = []): string {
  const body = normalizeContextText(text);
  const lines = files.map(renderFileLine);
  if (!body && !lines.length) return '';
  const inner = [body ? neutralizeInner(body) : '', ...lines].filter((part) => part).join('\n');
  const title = escapeAttr(sourceTitle.trim() || 'Untitled conversation');
  return `<${HANDOFF_CONTEXT_TAG} from_conversation="${title}">\n${CONTEXT_NOTE}\n${inner}\n</${HANDOFF_CONTEXT_TAG}>`;
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

/**
 * A delivery as the receiving model reads it (`render_delivery_block` in `context.py`): the same
 * wrapper, then one self-closing line per artifact. Used for the live bubble, so it reads exactly
 * as the bubble a reload renders from the stored message. Pinned to the Python by the fixture.
 */
export function renderDeliveryBlock(
  text: string,
  fromTitle: string,
  artifacts: readonly DeliveryArtifact[] = [],
  files: readonly FileLine[] = [],
): string {
  const body = neutralizeCloseTags(normalizeContextText(text));
  const lines = [
    ...artifacts.map(
      (a) =>
        `<${ARTIFACT_TAG} id="${escapeAttr(a.artifactId)}" version="${Math.trunc(a.version)}" ` +
        `title="${escapeAttr(a.title)}" type="${escapeAttr(a.contentType)}"/>`,
    ),
    ...files.map(renderFileLine),
  ];
  const inner = [body, ...lines].filter((part) => part).join('\n');
  if (!inner) return '';
  const title = escapeAttr(fromTitle.trim() || 'Untitled conversation');
  return `<${HANDOFF_CONTEXT_TAG} from_conversation="${title}">\n${CONTEXT_NOTE}\n${inner}\n</${HANDOFF_CONTEXT_TAG}>`;
}

/** The user message with the cards before it, each followed by a blank line (`join_delivery_blocks`). */
export function joinDeliveryBlocks(blocks: readonly string[], message: string): string {
  const head = blocks.filter((b) => b).join('\n\n');
  if (!head) return message;
  return message.trim() ? `${head}\n\n${message}` : head;
}

const ARTIFACT_LINE = /^<artifact id="([^"]*)" version="(\d+)" title="([^"]*)" type="([^"]*)"\/>$/;
const FILE_LINE = /^<file upload_id="([^"]*)" name="([^"]*)" type="([^"]*)" size="([^"]*)"\/>$/;

/** A size label back to bytes, for display only (the label is what the line carries). */
function sizeFromLabel(label: string): number {
  const [amount, unit] = label.split(' ');
  const n = Number(amount);
  if (!Number.isFinite(n)) return 0;
  return Math.round(unit === 'MB' ? n * 1_048_576 : unit === 'KB' ? n * 1024 : n);
}

/**
 * The delivery blocks a user message opens with, read back for its bubble, and what follows them.
 *
 * Only whole blocks at the very start count, in the exact shape the server writes, so text the
 * user typed can never be read as a card. Tags a body had defused are shown as written.
 */
export function extractDeliveryBlocks(text: string): { blocks: DeliveryBlock[]; rest: string } {
  const blocks: DeliveryBlock[] = [];
  const opener = `<${HANDOFF_CONTEXT_TAG} from_conversation="`;
  const closer = `\n</${HANDOFF_CONTEXT_TAG}>`;
  let rest = text;
  while (rest.startsWith(opener)) {
    const end = rest.indexOf(closer);
    if (end < 0) break;
    const lines = rest.slice(0, end).split('\n');
    const tag = lines[0];
    if (!tag.endsWith('">') || lines[1] !== CONTEXT_NOTE) break;
    const artifacts: DeliveryArtifact[] = [];
    const files: FileLine[] = [];
    let bodyEnd = lines.length;
    while (bodyEnd > 2) {
      const file = FILE_LINE.exec(lines[bodyEnd - 1]);
      if (!file) break;
      files.unshift({
        uploadId: unescapeAttr(file[1]),
        filename: unescapeAttr(file[2]),
        mimeType: unescapeAttr(file[3]),
        sizeBytes: sizeFromLabel(file[4]),
      });
      bodyEnd--;
    }
    while (bodyEnd > 2) {
      const match = ARTIFACT_LINE.exec(lines[bodyEnd - 1]);
      if (!match) break;
      artifacts.unshift({
        artifactId: unescapeAttr(match[1]),
        version: Number(match[2]),
        title: unescapeAttr(match[3]),
        contentType: unescapeAttr(match[4]),
      });
      bodyEnd--;
    }
    blocks.push({
      fromTitle: unescapeAttr(tag.slice(opener.length, -2)),
      text: restoreDefusedTags(lines.slice(2, bodyEnd).join('\n')),
      artifacts,
      files,
    });
    rest = rest.slice(end + closer.length).replace(/^\n\n/, '');
  }
  return { blocks, rest };
}

function unescapeAttr(value: string): string {
  return value.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

/** Show a body's defused tags as the sender wrote them; this is display, never a prompt. */
function restoreDefusedTags(text: string): string {
  let out = text.split(`<\\/${EXCERPT_TAG}`).join(`</${EXCERPT_TAG}`);
  for (const tag of FORGEABLE_TAGS) {
    out = out.split(`<\\/${tag}`).join(`</${tag}`).split(`<\\${tag}`).join(`<${tag}`);
  }
  return out;
}
