import type { ToolCallDisplay } from './tool-rail.model';

/**
 * Memory tool calls, read for the rail.
 *
 * A Shared Project's harness reads and writes its memory spaces through
 * `memory_*` tools (`agents/builtin_tools/memory_spaces/project_tools.py`).
 * The name pattern in `tool-summary.ts` can't place them, since "memory" is not
 * a verb, so a save rendered as "Ran memory save" with its list behind the raw
 * JSON. A save is the one step in a turn that outlives the conversation, so the
 * rail names it plainly, counts it in the header, and shows what was saved as
 * prose, much as it treats a span of reasoning.
 *
 * Everything here comes from the call's name, input and status, which the
 * client already holds: nothing to persist, nothing on the prompt, and a
 * reloaded conversation renders the same.
 */

export type MemoryCallKind = 'save' | 'update' | 'rename' | 'index' | 'propose' | 'read' | 'list' | 'query';

/** The index file's slug. A save to it rewrites the index, not a memory. */
const INDEX_SLUG = 'MEMORY.md';

/** An existing item's anchor. Only a file this conversation read carries them. */
const ANCHOR = /<!--\s*e:[^>]*-->/g;

function bareName(toolName: string): string {
  return (toolName ?? '').split('::').pop() ?? '';
}

function stringInput(call: ToolCallDisplay, key: string): string {
  const value = call.input?.[key];
  return typeof value === 'string' ? value : '';
}

/** What a memory call did, or null when the call isn't one. */
export function memoryCallKind(call: ToolCallDisplay): MemoryCallKind | null {
  switch (bareName(call.toolName)) {
    case 'memory_save': {
      if (stringInput(call, 'slug') === INDEX_SLUG) return 'index';
      if (stringInput(call, 'replaces').trim()) return 'rename';
      // A save that keeps anchors is sending back a file it read: an edit.
      return /<!--\s*e:/.test(stringInput(call, 'text')) ? 'update' : 'save';
    }
    case 'memory_propose':
      return 'propose';
    case 'memory_read':
      return 'read';
    case 'memory_list':
      return 'list';
    case 'memory_query':
      return 'query';
    default:
      return null;
  }
}

/** True for a call that writes (or proposes writing) a memory file. */
export function isMemoryWriteKind(kind: MemoryCallKind | null): boolean {
  return kind === 'save' || kind === 'update' || kind === 'rename' || kind === 'index' || kind === 'propose';
}

const LINES: Record<MemoryCallKind, { running: string; ok: string; failed: string }> = {
  save: { running: 'Saving a memory', ok: 'Saved a memory', failed: "Couldn't save a memory" },
  update: { running: 'Updating a memory', ok: 'Updated a memory', failed: "Couldn't update a memory" },
  rename: { running: 'Renaming a memory', ok: 'Renamed a memory', failed: "Couldn't rename a memory" },
  index: {
    running: 'Updating the memory index',
    ok: 'Updated the memory index',
    failed: "Couldn't update the memory index",
  },
  propose: {
    running: 'Proposing a memory change',
    ok: 'Proposed a memory change',
    failed: "Couldn't propose a memory change",
  },
  read: { running: 'Reading a memory', ok: 'Read a memory', failed: "Couldn't read a memory" },
  list: { running: 'Checking memories', ok: 'Checked memories', failed: "Couldn't check memories" },
  query: { running: 'Searching memories', ok: 'Searched memories', failed: "Couldn't search memories" },
};

/** The rail's line for one memory call. */
export function describeMemoryCall(kind: MemoryCallKind, state: 'running' | 'ok' | 'failed'): string {
  return LINES[kind][state];
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : `${n} ${many}`;
}

/** The rollup fragments, in the order a turn usually does them. */
const ROLLUP: readonly { kinds: MemoryCallKind[]; phrase: (n: number) => string }[] = [
  { kinds: ['list'], phrase: () => 'checked memories' },
  { kinds: ['query'], phrase: () => 'searched memories' },
  { kinds: ['read'], phrase: (n) => `read ${plural(n, 'a memory', 'memories')}` },
  { kinds: ['save'], phrase: (n) => `saved ${plural(n, 'a memory', 'memories')}` },
  { kinds: ['update', 'rename'], phrase: (n) => `updated ${plural(n, 'a memory', 'memories')}` },
  { kinds: ['index'], phrase: () => 'updated the memory index' },
  { kinds: ['propose'], phrase: (n) => `proposed ${plural(n, 'a memory change', 'memory changes')}` },
];

/**
 * One line for finished memory calls that did not fail, in the shape of
 * "Read a memory, saved 2 memories". Empty when there are none.
 */
export function memoryRollup(kinds: readonly MemoryCallKind[]): string {
  const parts = ROLLUP.flatMap(({ kinds: match, phrase }) => {
    const n = kinds.filter((k) => match.includes(k)).length;
    return n ? [phrase(n)] : [];
  });
  const line = parts.join(', ');
  return line ? line.charAt(0).toUpperCase() + line.slice(1) : '';
}

/**
 * The header readout for a rail's memory writes ("saved a memory"), in the
 * lower case of the other readouts beside the headline. Reads and listings
 * are left out: only a write outlives the conversation.
 */
export function memoryWriteLabel(kinds: readonly MemoryCallKind[]): string | null {
  const line = memoryRollup(kinds.filter(isMemoryWriteKind));
  return line ? line.charAt(0).toLowerCase() + line.slice(1) : null;
}

/** What a write saved, shown as prose when its row is opened. */
export interface MemoryWriteDetail {
  /** "mine", "project", or empty when the tool takes no scope (a proposal). */
  scope: string;
  slug: string;
  /** The file's index line. */
  description: string;
  /** The file's items, without their "- " markers or anchors. */
  items: string[];
}

/** The detail of a memory write, or null when its input isn't a memory list. */
export function memoryWriteDetail(call: ToolCallDisplay): MemoryWriteDetail | null {
  if (!isMemoryWriteKind(memoryCallKind(call))) return null;
  const text = stringInput(call, 'text');
  const items = text
    .split('\n')
    .map((line) => line.replace(ANCHOR, '').replace(/^\s*[-*]\s+/, '').trim())
    .filter(Boolean);
  if (!items.length) return null;
  return {
    scope: stringInput(call, 'scope') || (memoryCallKind(call) === 'propose' ? 'project' : ''),
    slug: stringInput(call, 'slug'),
    description: stringInput(call, 'description').trim(),
    items,
  };
}
