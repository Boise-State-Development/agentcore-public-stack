import { ItemProvenance, MemoryEntry } from '../models/project.model';

/**
 * Plain helpers for the Memory tab (shared-projects 2.8): reading memory files as items,
 * comparing two versions of one, resolving `[[links]]` and wording provenance.
 *
 * The backend owns the file format (`apis/shared/memory/format.py`); these only read it.
 * An item is a top-level `- ` line plus its indented continuation lines, ending in an
 * `<!-- e:xxxxxxxx -->` anchor the system manages.
 */

/** The reviewer's note on a decision (`MAX_PROPOSAL_NOTE_CHARS`). */
export const MAX_PROPOSAL_NOTE_CHARS = 280;

/** The reserved index file every space has. */
export const INDEX_SLUG = 'MEMORY.md';

const TRAILING_ANCHOR = /\s*<!--\s*e:([0-9a-z]{8})\s*-->\s*$/i;
const FRONTMATTER = /^---\n[\s\S]*?\n---\n?/;
const LINK = /\[\[([^\]\n]+?)\]\]/g;

export interface ParsedItem {
  text: string;
  anchor: string | null;
}

/** A file's items, in order. Frontmatter is skipped; a stray prose line reads as its own item. */
export function parseItems(text: string | null | undefined): ParsedItem[] {
  const body = (text ?? '').replace(/\r\n?/g, '\n').replace(FRONTMATTER, '');
  const blocks: string[][] = [];
  for (const line of body.split('\n')) {
    if (line.startsWith('- ') || line.trimEnd() === '-') blocks.push([line.slice(2)]);
    else if (blocks.length && line.startsWith('  ')) blocks[blocks.length - 1].push(line.slice(2));
    else if (blocks.length && line.startsWith('\t')) blocks[blocks.length - 1].push(line.slice(1));
    else if (!line.trim()) blocks[blocks.length - 1]?.push('');
    else blocks.push([line.trim()]);
  }
  return blocks
    .map(lines => {
      const joined = lines.join('\n').trimEnd();
      const match = TRAILING_ANCHOR.exec(joined);
      return {
        text: (match ? joined.slice(0, match.index) : joined).trim(),
        anchor: match ? match[1].toLowerCase() : null,
      };
    })
    .filter(item => item.text.length > 0);
}

export interface DiffItem {
  kind: 'same' | 'added' | 'removed';
  text: string;
}

export interface SideBySide {
  /** The file as it is: kept items and the ones the change removes. */
  current: DiffItem[];
  /** The file after the change: kept items and the ones it adds. */
  proposed: DiffItem[];
}

/**
 * Two versions of a file, item by item, with anchors hidden. Items are matched by their
 * text, so an edited item reads as one removed and one added: enough to review a file of
 * one-line facts without a diff library.
 */
export function compareItems(current: string | null | undefined, proposed: string): SideBySide {
  const before = parseItems(current).map(i => i.text);
  const after = parseItems(proposed).map(i => i.text);
  const kept = new Set(before);
  const now = new Set(after);
  return {
    current: before.map(text => ({ kind: now.has(text) ? 'same' : 'removed', text })),
    proposed: after.map(text => ({ kind: kept.has(text) ? 'same' : 'added', text })),
  };
}

/** How many items a comparison adds and removes. */
export function changeCounts(diff: SideBySide): { added: number; removed: number } {
  return {
    added: diff.proposed.filter(i => i.kind === 'added').length,
    removed: diff.current.filter(i => i.kind === 'removed').length,
  };
}

/** Tokens as the harness budgets an index: four characters each. */
export function estimateTokens(text: string | null | undefined): number {
  return Math.ceil((text ?? '').length / 4);
}

export type TextPart = { kind: 'text'; text: string } | { kind: 'link'; name: string; slug: string | null };

/**
 * `text` split into plain runs and `[[links]]`. A link resolves, ignoring case, to a file's
 * name or one of its aliases, or to the index; `slug` is null when nothing matches.
 */
export function linkParts(text: string, entries: readonly MemoryEntry[]): TextPart[] {
  const parts: TextPart[] = [];
  let last = 0;
  for (const match of text.matchAll(LINK)) {
    const at = match.index ?? 0;
    if (at > last) parts.push({ kind: 'text', text: text.slice(last, at) });
    const name = match[1].trim();
    parts.push({ kind: 'link', name, slug: resolveLink(name, entries) });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push({ kind: 'text', text: text.slice(last) });
  return parts;
}

export function resolveLink(name: string, entries: readonly MemoryEntry[]): string | null {
  const key = name.trim().toLowerCase();
  if (key === INDEX_SLUG.toLowerCase()) return INDEX_SLUG;
  const hit = entries.find(e => e.slug.toLowerCase() === key || e.aliases.some(a => a.toLowerCase() === key));
  return hit?.slug ?? null;
}

export interface ProvenanceLine {
  /** One sentence: who put the item here and how. */
  text: string;
  /** When it last changed, as an ISO timestamp; empty when unknown. */
  at: string;
  /** The caller's own task it came from, which they can open. */
  sessionId: string | null;
}

/**
 * Where an item came from, in one line. Shown inline under each item rather than on hover,
 * because touch screens have no hover (shared-projects §6).
 */
export function describeProvenance(
  provenance: ItemProvenance | null | undefined,
  people: Record<string, string>,
  myEmail: string | null,
): ProvenanceLine {
  if (!provenance) return { text: 'Added before item history was kept', at: '', sessionId: null };
  const me = (myEmail ?? '').toLowerCase();
  const who = (email: string | null | undefined) =>
    !email ? 'someone' : email.toLowerCase() === me ? 'you' : people[email] || email;
  const at = provenance.restoredAt || provenance.updatedAt || provenance.addedAt || '';
  const fromTask = provenance.sourceSessionId ? ' from a task' : '';
  const editor = provenance.updatedBy && provenance.updatedBy !== provenance.addedBy ? provenance.updatedBy : null;

  let text: string;
  if (provenance.restoredBy) {
    text = `Restored from the archive by ${who(provenance.restoredBy)}`;
  } else if (provenance.proposedBy || provenance.proposalId) {
    const approver = provenance.approvedBy ? `, approved by ${who(provenance.approvedBy)}` : '';
    text = `Proposed by ${who(provenance.proposedBy ?? provenance.addedBy)}${fromTask}${approver}`;
  } else if (editor) {
    text = `Added by ${who(provenance.addedBy)}, last edited by ${who(editor)}${fromTask}`;
  } else {
    text = `${provenance.sourceSessionId ? 'Saved' : 'Added'} by ${who(provenance.addedBy)}${fromTask}`;
  }
  const changedBy = provenance.updatedBy || provenance.addedBy;
  const mine = !!me && changedBy?.toLowerCase() === me;
  return { text: capitalize(text), at, sessionId: mine ? (provenance.sourceSessionId ?? null) : null };
}

/** Everyone who added, changed, proposed, approved or restored an item in the file. */
export function contributors(provenances: readonly (ItemProvenance | null | undefined)[]): string[] {
  const seen = new Set<string>();
  for (const p of provenances) {
    if (!p) continue;
    for (const email of [p.addedBy, p.updatedBy, p.proposedBy, p.approvedBy, p.restoredBy]) {
      if (email) seen.add(email);
    }
  }
  return [...seen];
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
