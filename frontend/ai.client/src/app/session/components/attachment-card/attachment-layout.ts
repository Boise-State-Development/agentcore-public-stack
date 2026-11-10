/** Files a composer shows as cards before it folds them into a summary. */
export const COMPOSER_RAIL_LIMIT = 5;

/** Files a sent message lays out in full before it shows a "+N" tile. */
export const MESSAGE_MOSAIC_LIMIT = 5;

/** Cards a folded message mosaic shows ahead of its "+N" tile. */
export const MESSAGE_MOSAIC_PREVIEW = 3;

/**
 * How many columns the first card of a mosaic spans, so every row is full:
 * it takes whatever the last row would otherwise leave empty. With two
 * columns, 3 files are one wide card and a pair, 4 are two pairs, and 5 are
 * one wide card and two pairs.
 */
export function mosaicLeadSpan(count: number, columns: number): number {
  if (count <= 1 || columns <= 1) return 1;
  const remainder = count % columns;
  return remainder === 0 ? 1 : columns - remainder + 1;
}
