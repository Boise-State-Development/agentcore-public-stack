/**
 * Chart color constants for Chart.js integration.
 *
 * These colors are resolved hex/rgba strings (not CSS custom properties or Tailwind classes)
 * because Chart.js operates on the canvas API at render time and cannot consume CSS variables
 * directly. Values are derived from Tailwind's palette (OKLCH → sRGB hex conversion).
 *
 * Usage:
 * - Categorical series (indexed palette): `CHART_COLORS.categoricalPalette`
 * - Semantic series (cost/requests lines): `CHART_COLORS.cost` and `CHART_COLORS.requests`
 * - Chrome/UI elements: `CHART_COLORS.chrome`
 */

/**
 * Categorical palette for indexed series (10 distinct, well-separated colors).
 * Used for pie/bar charts where series are distinguished by index.
 * Matches Tailwind's steps (Blue-500, Emerald-500, Amber-500, Red-500, Violet-500,
 * Pink-500, Cyan-500, Lime-500, Orange-500, Indigo-500).
 */
export const CHART_CATEGORICAL_PALETTE = [
  '#3b82f6', // blue-500
  '#10b981', // emerald-500
  '#f59e0b', // amber-500
  '#ef4444', // red-500
  '#8b5cf6', // violet-500
  '#ec4899', // pink-500
  '#06b6d4', // cyan-500
  '#84cc16', // lime-500
  '#f97316', // orange-500
  '#6366f1', // indigo-500
] as const;

/**
 * Semantic data series colors.
 */
export const CHART_SERIES_COLORS = {
  /** Cost line color (blue-500) */
  cost: '#3b82f6' as const,
  /** Requests line color (emerald-500) */
  requests: '#10b981' as const,
} as const;

/**
 * Chart chrome and UI element colors (backgrounds, text, grids).
 * These are intentionally resolved hex values for direct Chart.js consumption.
 */
export const CHART_CHROME_COLORS = {
  light: {
    /** Tooltip/dropdown background */
    background: '#ffffff' as const,
    /** Tooltip title text */
    titleText: '#111827' as const, // gray-900
    /** Tooltip body text */
    bodyText: '#4b5563' as const,
    /** Tooltip border */
    border: '#e5e7eb' as const, // gray-200
    /** Axis label text */
    axisText: '#6b7280' as const, // gray-500
    /** Grid lines */
    gridLine: 'rgba(0, 0, 0, 0.1)' as const,
  },
  dark: {
    /** Tooltip/dropdown background */
    background: '#1f2937' as const, // gray-800
    /** Tooltip title text */
    titleText: '#ffffff' as const,
    /** Tooltip body text */
    bodyText: '#d1d5db' as const, // gray-300
    /** Tooltip border */
    border: '#374151' as const, // gray-700
    /** Axis label text */
    axisText: '#9ca3af' as const, // gray-400
    /** Grid lines */
    gridLine: 'rgba(255, 255, 255, 0.1)' as const,
  },
} as const;

/**
 * Fill colors for line chart datasets (used with opacity).
 * Matches the line colors with 10% opacity.
 */
export const CHART_FILL_COLORS = {
  cost: 'rgba(59, 130, 246, 0.1)' as const, // blue-500 @ 10%
  requests: 'rgba(16, 185, 129, 0.1)' as const, // emerald-500 @ 10%
} as const;

/**
 * Helper to get the appropriate chrome colors based on dark mode.
 * @param isDarkMode - Whether dark mode is active
 * @returns Chrome color set for the active mode
 */
export function getChromeColorsForMode(isDarkMode: boolean) {
  return isDarkMode ? CHART_CHROME_COLORS.dark : CHART_CHROME_COLORS.light;
}

/**
 * Helper to get a color from the categorical palette by index (with wrapping).
 * @param index - The index in the series
 * @returns Hex color string
 */
export function getCategoricalColor(index: number): string {
  return CHART_CATEGORICAL_PALETTE[index % CHART_CATEGORICAL_PALETTE.length];
}
