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

/** A run result part's step, as the server stores it (workflow-runs spec §7). Only `done` carries a result. */
export type DeliveryPartStatus = 'done' | 'stopped' | 'needs_input' | 'running';

/**
 * What a part with no result says, inside its block (`DELIVERY_PART_STATUS_NOTES` in `context.py`).
 * Exported so a sent bubble can tell such a part from an answer.
 */
export const DELIVERY_PART_STATUS_NOTES: Readonly<Record<Exclude<DeliveryPartStatus, 'done'>, string>> = {
  stopped: 'This Agent stopped before it finished, so there is no result from it.',
  needs_input: 'This Agent is still waiting on the user, so there is no result from it yet.',
  running: 'This Agent was still working when the results were brought back, so there is no result from it yet.',
};

/**
 * The same notes for a prompt step's part, which has no Agent (`PROMPT_PART_STATUS_NOTES` in
 * `context.py`). A prompt step never waits on the user; `needs_input` is here so every status has one.
 */
export const PROMPT_PART_STATUS_NOTES: Readonly<Record<Exclude<DeliveryPartStatus, 'done'>, string>> = {
  stopped: 'This step stopped before it finished, so there is no result from it.',
  needs_input: 'This step is still waiting on the user, so there is no result from it yet.',
  running: 'This step was still working when the results were brought back, so there is no result from it yet.',
};

/** One part of a run result as its block is rendered (`DeliveryPartBlock` in `context.py`). */
export interface DeliveryPartBlockInput {
  text: string;
  agentName: string;
  fromTitle: string;
  status: DeliveryPartStatus;
  artifacts?: readonly DeliveryArtifact[];
  files?: readonly FileLine[];
  /** A prompt step's part: its note names a step, not an Agent (`PROMPT_PART_STATUS_NOTES`). */
  prompt?: boolean;
}

/** What a part's block names: the Agent, then its conversation's title (`delivery_part_label`). */
export function deliveryPartLabel(agentName: string, fromTitle: string): string {
  const agent = agentName.trim();
  const title = fromTitle.trim();
  if (!agent || !title) return agent || title;
  // A run step's title already leads with its Agent (`run_step_title`), so the name is written once.
  if (agent === title || title.startsWith(`${agent} · `)) return title;
  return `${agent} · ${title}`;
}

/**
 * A part's conversation title without its Agent's name in front, for a line that already shows the
 * Agent (the composer's result card). Empty when the title says nothing more than the name.
 */
export function partTitleAfterAgent(agentName: string, fromTitle: string): string {
  const agent = agentName.trim();
  const title = fromTitle.trim();
  if (!title || title === agent) return '';
  const prefix = `${agent} · `;
  return agent && title.startsWith(prefix) ? title.slice(prefix.length) : title;
}

/**
 * A run result as the receiving model reads it (`render_delivery_parts` in `context.py`): one
 * delivery block per part, in step order, joined by a blank line. A part with no result is named
 * with its status note; a finished part the user emptied gives no block. Pinned by the fixture.
 */
export function renderDeliveryParts(parts: readonly DeliveryPartBlockInput[]): string {
  return parts
    .map((part) => {
      const label = deliveryPartLabel(part.agentName, part.fromTitle);
      return part.status === 'done'
        ? renderDeliveryBlock(part.text, label, part.artifacts ?? [], part.files ?? [])
        : renderDeliveryBlock((part.prompt ? PROMPT_PART_STATUS_NOTES : DELIVERY_PART_STATUS_NOTES)[part.status] ?? '', label);
    })
    .filter((block) => block)
    .join('\n\n');
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

/** A run's step Agents, in step order: what tells a run result's blocks from separate cards. */
export interface RunResultAgents {
  runId: string;
  agentNames: readonly string[];
  /**
   * Per step, in the same order: a prompt step's name, or `null` for an Agent step. A prompt
   * step's block is labelled "{model} · {step}" (`prompt_part_names`), so it is matched on that
   * whole label, never on the model alone, which several prompt steps can share (§21.3).
   */
  promptSteps?: readonly (string | null)[];
  /**
   * Per step, in the same order: true for a checklist step (§22.3). Its block is labelled like a
   * prompt step's, by its model and its step, but "Checklist step" when the run kept no name
   * (`CHECKLIST_STEP_FALLBACK_NAME`). Its name belongs in `promptSteps` too.
   */
  checklistSteps?: readonly boolean[];
}

/** One part of a run result read back for a sent bubble. */
export interface RunResultPartBlock {
  agentName: string;
  /** `done` for an answer; else the status whose note the block holds (it was not included). */
  status: DeliveryPartStatus;
  block: DeliveryBlock;
  /** A prompt or checklist step's part: the step's name. Its `agentName` is then the model's name. */
  promptStep?: string;
  /** A checklist step's part (§22.3), named "{step} · Checklist · {model}". */
  checklist?: boolean;
}

/** What a sent bubble shows for its delivery blocks: separate cards, and any run result as one. */
export type BubbleDelivery = { kind: 'card'; block: DeliveryBlock } | { kind: 'run'; runId: string; parts: RunResultPartBlock[] };

/**
 * Group a message's delivery blocks (`extractDeliveryBlocks`) for its bubble (workflow-runs spec
 * §20.3): a run result's blocks, which the model reads one per part, show as one "From *N* Agents"
 * ("*N* steps" once one is a prompt step).
 *
 * The stored text carries no run marker, so a run's blocks are recognised by what
 * `renderDeliveryParts` writes: consecutive blocks each labelled for the next of one run's step
 * Agents in step order (`deliveryPartLabel`: the Agent, or "Agent · title"; a prompt step's whole
 * "model · step" label, since its model alone can't tell it from another step on that model). A finished part the
 * user emptied gave no block, so steps may be skipped, never reordered. A lone block is grouped
 * only for a run of one, so an ordinary card from a conversation named like an Agent stays a card.
 * Anything not matched is a card, as before.
 */
export function groupRunResults(blocks: readonly DeliveryBlock[], runs: readonly RunResultAgents[]): BubbleDelivery[] {
  const out: BubbleDelivery[] = [];
  let i = 0;
  while (i < blocks.length) {
    let best: { runId: string; parts: RunResultPartBlock[] } | null = null;
    for (const run of runs) {
      const parts = matchRun(blocks, i, run.agentNames, run.promptSteps ?? [], run.checklistSteps ?? []);
      const minimum = Math.min(2, run.agentNames.length);
      if (parts.length >= minimum && parts.length > (best?.parts.length ?? 0)) best = { runId: run.runId, parts };
    }
    if (best) {
      out.push({ kind: 'run', runId: best.runId, parts: best.parts });
      i += best.parts.length;
    } else {
      out.push({ kind: 'card', block: blocks[i] });
      i += 1;
    }
  }
  return out;
}

function matchRun(
  blocks: readonly DeliveryBlock[],
  start: number,
  agentNames: readonly string[],
  promptSteps: readonly (string | null)[],
  checklistSteps: readonly boolean[],
): RunResultPartBlock[] {
  const parts: RunResultPartBlock[] = [];
  let step = 0;
  for (let j = start; j < blocks.length && step < agentNames.length; j++) {
    const label = blocks[j].fromTitle;
    let found = -1;
    for (let k = step; k < agentNames.length; k++) {
      if (labelMatchesStep(label, agentNames[k].trim(), promptSteps[k] ?? null, !!checklistSteps[k])) {
        found = k;
        break;
      }
    }
    if (found < 0) break;
    const promptStep = promptSteps[found];
    parts.push({
      agentName: agentNames[found],
      status: partStatusOf(blocks[j], promptStep != null),
      block: blocks[j],
      ...(promptStep != null ? { promptStep } : {}),
      ...(promptStep != null && checklistSteps[found] ? { checklist: true } : {}),
    });
    step = found + 1;
  }
  return parts;
}

/** A prompt step's block when the run kept no step name for it (`PROMPT_STEP_FALLBACK_NAME`). */
const PROMPT_STEP_FALLBACK_NAME = 'Prompt step';
/** A checklist step's block when the run kept no step name for it (`CHECKLIST_STEP_FALLBACK_NAME`). */
const CHECKLIST_STEP_FALLBACK_NAME = 'Checklist step';

function labelMatchesStep(label: string, agent: string, promptStep: string | null, checklist: boolean): boolean {
  if (promptStep !== null) {
    const fallback = checklist ? CHECKLIST_STEP_FALLBACK_NAME : PROMPT_STEP_FALLBACK_NAME;
    return label === deliveryPartLabel(agent, promptStep.trim() || fallback);
  }
  return !!agent && (label === agent || label.startsWith(`${agent} · `));
}

function partStatusOf(block: DeliveryBlock, prompt: boolean): DeliveryPartStatus {
  if (block.artifacts.length || block.files.length) return 'done';
  // A prompt part sent before its notes named a step still reads as the status it was.
  const notes = prompt ? [PROMPT_PART_STATUS_NOTES, DELIVERY_PART_STATUS_NOTES] : [DELIVERY_PART_STATUS_NOTES];
  for (const [status, note] of notes.flatMap((n) => Object.entries(n))) {
    if (block.text === note) return status as DeliveryPartStatus;
  }
  return 'done';
}
