/**
 * Initials for a display name, so a card or row reads as an object rather than
 * a line of text.
 *
 * Strips anything that isn't a letter or a space first: catalog display names
 * carry underscores, colons and version suffixes (`gmail_employee`,
 * `canvas_faculty`), and a monogram built from punctuation is noise.
 */
export function monogramFor(displayName: string): string {
  const initials = displayName
    .replace(/[^A-Za-z ]/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(word => word[0])
    .join('');
  return initials.toUpperCase() || '?';
}
