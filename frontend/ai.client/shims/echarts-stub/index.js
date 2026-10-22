/**
 * Build-time stand-in for `echarts`. See README.md for why this exists.
 *
 * Only the two entry points `pptx-preview` actually calls are provided.
 */

/** Thrown when a deck really does contain a native OOXML chart. */
function unsupported() {
  throw new Error(
    'pptx-preview: native OOXML charts are not supported in this build',
  );
}

export function init() {
  return unsupported();
}

export function use() {
  // No-op. Registration is meaningless without a charting runtime, and
  // it may be called before the deck is known to contain a chart —
  // throwing here would fail decks that have none.
}

export default { init, use };
