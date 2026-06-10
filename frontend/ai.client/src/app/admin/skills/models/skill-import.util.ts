/**
 * SKILL.md import helpers (PR-5, §0.4).
 *
 * Import is a *writing shortcut*: it prefills the create form from an existing
 * skill bundle's `SKILL.md`. Tool bindings are NOT imported (off-the-shelf
 * skills carry no reference to our catalog) and scripts are out of scope —
 * only the authored knowledge (frontmatter + instructions body) is mapped.
 *
 * These are pure functions so the parsing/slugifying rules are unit-tested
 * without a DOM (the file-reading wrapper lives in the form page).
 */

import { SKILL_ID_PATTERN } from './admin-skill.model';

/**
 * The prefill fields extracted from a SKILL.md.
 */
export interface ParsedSkillMarkdown {
  /** Frontmatter `name` (raw), or '' if absent. */
  name: string;
  /** Frontmatter `description`, or '' if absent. */
  description: string;
  /** The markdown body after the frontmatter (→ instructions). */
  instructions: string;
}

/**
 * Parse a SKILL.md into prefill fields.
 *
 * Recognises a leading YAML frontmatter block delimited by `---` lines and
 * extracts the `name` / `description` keys (a deliberately small subset — the
 * ecosystem SKILL.md frontmatter only uses simple `key: value` scalars).
 * Everything after the closing `---` is the instructions body. With no
 * frontmatter, the whole text becomes the instructions.
 */
export function parseSkillMarkdown(text: string): ParsedSkillMarkdown {
  const normalized = text.replace(/\r\n/g, '\n');
  const fm = extractFrontmatter(normalized);
  if (!fm) {
    return { name: '', description: '', instructions: normalized.trim() };
  }
  return {
    name: fm.fields['name'] ?? '',
    description: fm.fields['description'] ?? '',
    instructions: fm.body.trim(),
  };
}

interface Frontmatter {
  fields: Record<string, string>;
  body: string;
}

function extractFrontmatter(text: string): Frontmatter | null {
  if (!text.startsWith('---\n')) {
    return null;
  }
  // Closing delimiter: a line that is exactly `---`.
  const closeMatch = text.match(/\n---[ \t]*(\n|$)/);
  if (!closeMatch || closeMatch.index === undefined) {
    return null;
  }
  const block = text.slice(4, closeMatch.index);
  const body = text.slice(closeMatch.index + closeMatch[0].length);

  const fields: Record<string, string> = {};
  for (const line of block.split('\n')) {
    const m = line.match(/^([A-Za-z0-9_-]+):\s?(.*)$/);
    if (m) {
      fields[m[1].toLowerCase()] = stripQuotes(m[2].trim());
    }
  }
  return { fields, body };
}

function stripQuotes(value: string): string {
  if (value.length >= 2) {
    const first = value[0];
    const last = value[value.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return value.slice(1, -1);
    }
  }
  return value;
}

/**
 * Best-effort conversion of a SKILL.md `name` (often hyphenated, e.g.
 * `pdf-tools`) into a valid skill_id (`^[a-z][a-z0-9_]{2,49}$`): lowercase,
 * non-`[a-z0-9_]` → `_`, collapse repeats, trim underscores, ensure it starts
 * with a letter, and clamp the length. The result may still be invalid (e.g.
 * too short) — the form validates and lets the admin correct it.
 */
export function slugifySkillId(name: string): string {
  let slug = name
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  // Must start with a letter.
  if (slug && !/^[a-z]/.test(slug)) {
    slug = `s_${slug}`;
  }
  // Clamp to the 50-char ceiling, then re-trim a trailing underscore the cut
  // may have left.
  if (slug.length > 50) {
    slug = slug.slice(0, 50).replace(/_+$/g, '');
  }
  return slug;
}

/**
 * Whether a candidate skill_id satisfies the backend pattern.
 */
export function isValidSkillId(skillId: string): boolean {
  return SKILL_ID_PATTERN.test(skillId);
}
