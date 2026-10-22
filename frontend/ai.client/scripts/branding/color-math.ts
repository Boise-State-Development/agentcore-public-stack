/**
 * Color_Math — shared sRGB <-> OKLab/OKLCH conversion and WCAG contrast
 * helpers, used by both the brand-color generator
 * (generate-brand-theme.ts) and the surface-color generator
 * (generate-surface-theme.ts), plus the config-side validation in
 * brand-config.normalize.ts.
 *
 * Extracted so the two generators (and the runtime validation bands for
 * `surfaces`) share one implementation instead of maintaining separate
 * copies of the same sRGB/OKLab math and hex-format regex.
 *
 * Conversions follow Björn Ottosson's OKLab definition; sRGB output is
 * gamut-clamped per channel, matching what a browser does when an
 * `oklch()` value lands outside the display gamut.
 */

/** Matches a 6-digit hex color, with an optional leading '#'. Case-insensitive. */
export const HEX_COLOR_REGEX = /^#?[0-9a-fA-F]{6}$/;

/** Normalize a hex input by ensuring it has a leading '#'. Assumes the value already matches HEX_COLOR_REGEX. */
export function normalizeHex(hex: string): string {
  return hex.startsWith('#') ? hex : `#${hex}`;
}

/** sRGB channel (0-1) -> linear-light value. */
export function srgbToLinear(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** Linear-light value -> sRGB channel, clamped to the displayable [0,1] range. */
export function linearToSrgb(c: number): number {
  const v = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
  return Math.min(1, Math.max(0, v));
}

/** Parse '#rrggbb' (or 'rrggbb') into sRGB channels in 0-1. */
export function hexToSrgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255) as [number, number, number];
}

/** OKLCH (l in 0-1, c, h in degrees) -> sRGB channels in 0-1, gamut-clamped. */
export function oklchToSrgb(l: number, c: number, hDeg: number): [number, number, number] {
  const hRad = (hDeg * Math.PI) / 180;
  const a = c * Math.cos(hRad);
  const b = c * Math.sin(hRad);

  const lp = l + 0.3963377774 * a + 0.2158037573 * b;
  const mp = l - 0.1055613458 * a - 0.0638541728 * b;
  const sp = l - 0.0894841775 * a - 1.291485548 * b;

  const lc = lp ** 3;
  const mc = mp ** 3;
  const sc = sp ** 3;

  return [
    linearToSrgb(4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc),
    linearToSrgb(-1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc),
    linearToSrgb(-0.0041960863 * lc - 0.7034186147 * mc + 1.707614701 * sc),
  ];
}

/** '#rrggbb' -> OKLCH { l (0-1), c, h (degrees, 0-360) }. */
export function hexToOklch(hex: string): { l: number; c: number; h: number } {
  const [r, g, b] = hexToSrgb(normalizeHex(hex)).map(srgbToLinear);

  const lp = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const mp = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const sp = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);

  const l = 0.2104542553 * lp + 0.793617785 * mp - 0.0040720468 * sp;
  const a = 1.9779984951 * lp - 2.428592205 * mp + 0.4505937099 * sp;
  const b2 = 0.0259040371 * lp + 0.7827717662 * mp - 0.808675766 * sp;

  const c = Math.hypot(a, b2);
  let h = (Math.atan2(b2, a) * 180) / Math.PI;
  if (h < 0) h += 360;

  return { l, c, h };
}

/** WCAG 2.1 relative luminance of sRGB channels in 0-1. */
export function relativeLuminance([r, g, b]: [number, number, number]): number {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

/** WCAG 2.1 contrast ratio between two sRGB colors. */
export function contrastRatio(a: [number, number, number], b: [number, number, number]): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** WCAG 2.1 AA contrast target for normal-size text (1.4.3). */
export const CONTRAST_TARGET_AA = 4.5;

/** Granularity of the accessible-lightness search, in OKLCH lightness units. */
const LIGHTNESS_SEARCH_STEP = 0.005;

/**
 * Find the OKLCH lightness delta that brings `hex` to at least `target`
 * contrast against `background`, holding chroma and hue fixed.
 *
 * Returns 0 when the color already passes. `direction` is 'darken' for
 * light backgrounds and 'lighten' for dark backgrounds.
 */
export function findAccessibleLightnessDelta(
  hex: string,
  background: [number, number, number],
  direction: 'darken' | 'lighten',
  target: number = CONTRAST_TARGET_AA,
): number {
  const { l, c, h } = hexToOklch(normalizeHex(hex));

  if (contrastRatio(oklchToSrgb(l, c, h), background) >= target) {
    return 0;
  }

  const sign = direction === 'darken' ? -1 : 1;
  const limit = direction === 'darken' ? l : 1 - l;

  for (let magnitude = LIGHTNESS_SEARCH_STEP; magnitude <= limit; magnitude += LIGHTNESS_SEARCH_STEP) {
    const candidate = l + sign * magnitude;
    if (contrastRatio(oklchToSrgb(candidate, c, h), background) >= target) {
      return Number((sign * magnitude).toFixed(3));
    }
  }

  return Number((sign * limit).toFixed(3));
}

/** Shortest-arc delta (in degrees, range (-180, 180]) from `from` to `to`. */
export function shortestHueDelta(from: number, to: number): number {
  let diff = (to - from) % 360;
  if (diff > 180) diff -= 360;
  if (diff < -180) diff += 360;
  return diff;
}
