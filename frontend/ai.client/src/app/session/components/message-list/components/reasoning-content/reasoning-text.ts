/**
 * Text helpers shared by the standalone reasoning block and the tool rail,
 * which folds reasoning that sits between tool calls into its own steps. Both
 * surfaces describe the same span, so they must word it the same way.
 */

/**
 * The last sentence in streamed `text` known to be finished, or null.
 *
 * A boundary is `.`, `!` or `?` followed by whitespace, or a line break, so
 * "2.8405" and "1.2%" stay whole. Whatever follows the last boundary is
 * still arriving, even if it ends in a full stop (that may be "2." before
 * "8405"), so it is never the answer. Takes the raw text: trimmed, a
 * finished final sentence would lose the whitespace that marks it finished.
 */
export function latestCompleteSentence(text: string): string | null {
  const parts = text.split(/(?<=[.!?])\s+|\n+/);
  parts.pop();
  for (let i = parts.length - 1; i >= 0; i--) {
    const sentence = parts[i].trim();
    if (sentence) return sentence;
  }
  return null;
}

/**
 * A measured thinking span for display, or null when there isn't one.
 *
 * Sub-second spans read "<1s" rather than rounding up to "1s": the whole
 * point of the readout is that it is measured, and 400ms is not a second.
 */
export function formatThinkingDuration(ms: number | undefined): string | null {
  if (typeof ms !== 'number' || ms < 0) return null;
  if (ms < 1000) return '<1s';

  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
