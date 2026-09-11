/**
 * Split an MCP tool description into the part a person wants and the part they
 * don't.
 *
 * Tool descriptions reach us as raw Python docstrings. `gmail_employee`'s
 * `send_message` carries its whole `Args:` block; `class_search`'s
 * `search_classes` runs to 3,019 characters of which 1,819 is reference
 * material. Across the prod catalog that is 64,812 characters over 108
 * descriptions, 58% of it below the first paragraph.
 *
 * The first paragraph is what the docstring convention puts the summary in, so
 * that is the human-facing half. Everything from the first section heading
 * onward is reference detail — available on demand, not in a picker row.
 *
 * This is presentation only. The description the *model* sees comes from the
 * live MCP server through the Strands tool registry, not from here, so nothing
 * in this file changes a single token of `toolConfig`.
 */

/**
 * Section headings used by the docstring conventions our MCP servers are
 * written in (Google style, mostly, with some NumPy-ish variants). Matched only
 * at the start of a line so a sentence containing "Note:" mid-paragraph doesn't
 * truncate the summary.
 */
const SECTION_HEADING =
  /^[ \t]*(Args|Arguments|Params|Parameters|Returns|Return|Yields|Raises|Throws|Note|NOTE|Notes|Example|Examples|Usage|Warning)\s*:/m;

export interface SplitDescription {
  /** The first paragraph — what a row or a card should show. */
  summary: string;
  /** Everything from the first section heading on, or '' when there is none. */
  detail: string;
}

/**
 * Returns `{summary, detail}`. A description with no section headings is all
 * summary, which is the common case for hand-written tools.
 */
export function splitToolDescription(
  description: string | null | undefined,
): SplitDescription {
  const text = (description ?? '').trim();
  if (!text) return { summary: '', detail: '' };

  const match = SECTION_HEADING.exec(text);
  if (!match || match.index === 0) {
    // index 0 means the description *opens* with a heading and has no prose
    // summary at all — better to show it whole than to show nothing.
    return { summary: text, detail: '' };
  }

  const summary = text.slice(0, match.index).trim();
  const detail = text.slice(match.index).trim();

  // A summary that collapsed to nothing is no use; fall back to the full text.
  if (!summary) return { summary: text, detail: '' };

  return { summary: collapse(summary), detail };
}

/**
 * Docstrings are wrapped to a source-code column and indented to their `def`.
 * Rendered as-is in a 320px pane that produces ragged half-lines, so the
 * summary is unwrapped into a single paragraph. The detail keeps its line
 * structure — an `Args:` block is a list, and unwrapping it would be worse.
 */
function collapse(text: string): string {
  return text.replace(/\s*\n\s*/g, ' ').replace(/[ \t]{2,}/g, ' ').trim();
}
