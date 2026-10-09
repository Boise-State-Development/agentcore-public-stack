import { ItemProvenance, MemoryEntry, MemoryLintFinding, ReplacedMemoryItem } from '../models/project.model';

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

export interface InlineRun {
  kind: 'plain' | 'strong' | 'em' | 'code';
  text: string;
}

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*]))/g;

/**
 * The inline markdown the assistant writes into items (`**bold**`, `*emphasis*`, `` `code` ``)
 * as runs, so a fact reads as text rather than asterisks. Anything else stays literal.
 */
export function inlineRuns(text: string): InlineRun[] {
  const runs: InlineRun[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) runs.push({ kind: 'plain', text: text.slice(last, at) });
    const token = match[0];
    if (token.startsWith('**')) runs.push({ kind: 'strong', text: token.slice(2, -2) });
    else if (token.startsWith('`')) runs.push({ kind: 'code', text: token.slice(1, -1) });
    else runs.push({ kind: 'em', text: token.slice(1, -1) });
    last = at + token.length;
  }
  if (last < text.length) runs.push({ kind: 'plain', text: text.slice(last) });
  return runs;
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
  /** The file a tidy-up's split moved it from (2.6c). */
  movedFrom: string | null;
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
  if (!provenance) return { text: 'Added before item history was kept', at: '', sessionId: null, movedFrom: null };
  const me = (myEmail ?? '').toLowerCase();
  const who = (email: string | null | undefined) =>
    !email ? 'someone' : email.toLowerCase() === me ? 'you' : people[email] || email;
  const at = provenance.restoredAt || provenance.updatedAt || provenance.addedAt || '';
  const fromTask = provenance.sourceSessionId ? ' from a task' : '';
  const editor = provenance.updatedBy && provenance.updatedBy !== provenance.addedBy ? provenance.updatedBy : null;

  let text: string;
  if (provenance.restoredBy) {
    text = `Restored by ${who(provenance.restoredBy)}`;
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
  return {
    text: capitalize(text),
    at,
    sessionId: mine ? (provenance.sourceSessionId ?? null) : null,
    movedFrom: provenance.movedFrom ?? null,
  };
}

/** Everyone who added, changed, proposed, approved or restored an item in the file. */
export function contributors(provenances: readonly (ItemProvenance | null | undefined)[]): string[] {
  const seen = new Set<string>();
  for (const p of provenances) {
    if (!p) continue;
    for (const email of [p.addedBy, p.updatedBy, p.proposedBy, p.approvedBy, p.restoredBy, p.movedBy]) {
      if (email) seen.add(email);
    }
  }
  return [...seen];
}

/**
 * An item's supersede marker (2.6c): what it replaced, in one phrase, counting only what the
 * archive holds and the marker shows. A merge's surviving item had its own text rewritten,
 * and its old wording is in the file's History, not the archive.
 */
export function replacedLabel(replaces: readonly ReplacedMemoryItem[]): string {
  const merged = replaces.filter(r => r.reason === 'merged').length;
  const superseded = replaces.length - merged;
  const parts: string[] = [];
  if (merged) parts.push(merged === 1 ? 'Merged with 1 other item' : `Merged with ${merged} other items`);
  if (superseded) parts.push(superseded === 1 ? 'Replaces an older item' : `Replaces ${superseded} older items`);
  return parts.join(' · ');
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// ---- the editor (2.8b) -------------------------------------------------

/** The format's limits (`apis/shared/memory/format.py`), checked as you type; the save checks them again. */
export const MAX_SLUG_CHARS = 128;
export const MAX_DESCRIPTION_CHARS = 160;
export const MAX_ALIASES = 10;
export const MAX_ALIAS_CHARS = 64;
const SLUG = /^[a-z0-9]+(?:[-_.][a-z0-9]+)*(?:\/[a-z0-9]+(?:[-_.][a-z0-9]+)*)*$/;
const ANY_ANCHOR = /<!--\s*e:/i;

/** Roughly what the system adds to a file on save (its frontmatter), for the editor's size estimate. */
export const FRONTMATTER_TOKEN_ESTIMATE = 40;

/** Items as the file text a save or a proposal takes, the way the backend renders them. */
export function renderItems(items: readonly ParsedItem[]): string {
  const out: string[] = [];
  for (const item of items) {
    const lines = item.text.replace(/\r\n?/g, '\n').replace(/^\n+|\n+$/g, '').split('\n');
    const rendered = [`- ${lines[0]}`, ...lines.slice(1).map(line => (line.trim() ? `  ${line}` : ''))];
    if (item.anchor) rendered[rendered.length - 1] += ` <!-- e:${item.anchor} -->`;
    out.push(...rendered);
  }
  return out.length ? `${out.join('\n')}\n` : '';
}

/** What is wrong with a new file's name, or null. */
export function slugProblem(slug: string, entries: readonly MemoryEntry[]): string | null {
  const name = slug.trim();
  if (!name) return 'Give the file a name.';
  if (name.toLowerCase() === INDEX_SLUG.toLowerCase()) return `${INDEX_SLUG} is the index; pick another name.`;
  if (name.length > MAX_SLUG_CHARS) return `Names are limited to ${MAX_SLUG_CHARS} characters.`;
  if (!SLUG.test(name)) return 'Use lowercase letters and digits joined by - _ or . (and / to group files, as in people/jane-doe).';
  if (resolveLink(name, entries)) return 'A file already has that name or alias.';
  return null;
}

/** What is wrong with a file's aliases, or null. `slug` is the file's own name. */
export function aliasProblem(aliases: readonly string[], slug: string, entries: readonly MemoryEntry[]): string | null {
  if (aliases.length > MAX_ALIASES) return `A file can have at most ${MAX_ALIASES} aliases.`;
  for (const alias of aliases) {
    if (alias.length > MAX_ALIAS_CHARS) return `“${alias.slice(0, 20)}…” is longer than ${MAX_ALIAS_CHARS} characters.`;
    if (/[[\]]/.test(alias)) return `“${alias}” can’t contain square brackets.`;
    const owner = resolveLink(alias, entries.filter(e => e.slug !== slug));
    if (owner) return `“${alias}” already names ${owner === INDEX_SLUG ? 'the index' : `“${owner}”`}.`;
  }
  return null;
}

/** Split the aliases field: comma-separated, trimmed, blanks dropped. */
export function splitAliases(value: string): string[] {
  return value
    .split(',')
    .map(a => a.trim().replace(/\s+/g, ' '))
    .filter(Boolean);
}

/**
 * What is wrong with one item's text, or null. A link must resolve, unless the item already
 * had it when the editor opened: the save refuses only new dead links, so an old one never
 * blocks an unrelated edit.
 */
export function itemProblem(text: string, original: string, names: readonly MemoryEntry[]): string | null {
  if (ANY_ANCHOR.test(text)) return 'Anchor comments (<!-- e:… -->) are managed for you; remove that part.';
  const before = new Set(linkParts(original, names).filter(p => p.kind === 'link').map(p => (p as { name: string }).name.toLowerCase()));
  const dead = linkParts(text, names)
    .filter((p): p is { kind: 'link'; name: string; slug: string | null } => p.kind === 'link' && !p.slug && !before.has(p.name.toLowerCase()))
    .map(p => `[[${p.name}]]`);
  if (dead.length) return `${dead.join(', ')} ${dead.length === 1 ? 'doesn’t match' : 'don’t match'} a file name or alias.`;
  return null;
}

/**
 * A save's content-check findings as a toast's line (2.7): the first sentence, and how many
 * more. Null when there are none.
 */
export function lintLine(findings: readonly MemoryLintFinding[] | null | undefined): string | null {
  if (!findings?.length) return null;
  const more = findings.length > 1 ? ` (and ${findings.length - 1} more)` : '';
  return `${findings[0].message}${more}`;
}
