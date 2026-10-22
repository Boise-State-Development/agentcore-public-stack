/**
 * One run of composer text, marked when it is a live `@mention` or `/skill`.
 *
 * The composer paints these into a mirror layer behind its textarea, which is
 * how a token reads as "this does something" without a chip beside the input
 * to say so.
 */
export interface ComposerSegment {
  text: string;
  mark: boolean;
}

/**
 * Where the live tokens sit in `text`.
 *
 * - The mention is the first `@Name` for the Agent this turn is handed to. The
 *   binding is a separate pick, not a read of the text, so only the picked
 *   Agent's own name marks — a hand-typed `@Someone` that was never picked is
 *   prose and stays plain.
 * - Skills use the same token rule as `findSkillCommands`: a `/slug` that
 *   starts a word and is one of `slugs`. Anything else with a slash (dates,
 *   paths, "and/or") stays plain.
 *
 * Ranges come back sorted and never overlap.
 */
export function findHighlightRanges(
  text: string,
  mentionName: string | null,
  slugs: readonly string[],
): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];

  if (mentionName) {
    const token = `@${mentionName}`;
    const at = text.indexOf(token);
    if (at !== -1) ranges.push([at, at + token.length]);
  }

  if (slugs.length > 0 && text.includes('/')) {
    const known = new Set(slugs.map(slug => slug.toLowerCase()));
    const pattern = /(^|\s)(\/([a-z0-9][a-z0-9-]*))(?![\w\-/])/gi;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(text)) !== null) {
      if (!known.has(match[3].toLowerCase())) continue;
      const start = match.index + match[1].length;
      ranges.push([start, start + match[2].length]);
    }
  }

  ranges.sort((a, b) => a[0] - b[0]);
  return ranges.filter((range, i) => i === 0 || range[0] >= ranges[i - 1][1]);
}

/** Split `text` into plain and marked runs, or null when nothing is marked. */
export function toSegments(
  text: string,
  ranges: ReadonlyArray<readonly [number, number]>,
): ComposerSegment[] | null {
  if (ranges.length === 0) return null;
  const segments: ComposerSegment[] = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) segments.push({ text: text.slice(cursor, start), mark: false });
    segments.push({ text: text.slice(start, end), mark: true });
    cursor = end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), mark: false });
  return segments;
}
