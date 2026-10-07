/** One run of a snippet: either plain text or a run that matches the query. */
export interface HighlightSegment {
  text: string;
  match: boolean;
}

/** Query words shorter than this are not emphasised (they match almost everywhere). */
const MIN_TERM_LENGTH = 2;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * The query's words, longest first, so a longer word wins where two overlap.
 * Case-insensitive; words shorter than two characters are dropped.
 */
export function queryTerms(query: string): string[] {
  const words = new Set(
    query
      .toLowerCase()
      .split(/\s+/)
      .filter(w => w.length >= MIN_TERM_LENGTH),
  );
  return [...words].sort((a, b) => b.length - a.length || a.localeCompare(b));
}

/**
 * Split `text` into runs, marking every occurrence of a query word, so a
 * template can wrap the matches in `<mark>` without any HTML in the data.
 * Text with nothing to mark comes back as one plain run.
 */
export function highlightSegments(text: string, query: string): HighlightSegment[] {
  const terms = queryTerms(query);
  if (!text) return [];
  if (terms.length === 0) return [{ text, match: false }];

  const pattern = new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'gi');
  const segments: HighlightSegment[] = [];
  let last = 0;
  for (const found of text.matchAll(pattern)) {
    const start = found.index ?? 0;
    if (start > last) segments.push({ text: text.slice(last, start), match: false });
    segments.push({ text: found[0], match: true });
    last = start + found[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), match: false });
  return segments;
}

/**
 * When a conversation last moved, as a result row shows it: "Just now",
 * "5m ago", "3h ago", "2d ago", then a short date. Same wording as the sidebar.
 */
export function formatLastMoved(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const diffMs = now.getTime() - date.getTime();
  const minutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(diffMs / 3600000);
  const days = Math.floor(diffMs / 86400000);
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}
