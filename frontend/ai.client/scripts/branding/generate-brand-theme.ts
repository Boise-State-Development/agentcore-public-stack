/**
 * Color_Scale_Generator (build-time).
 *
 * Pure transformation from the three Brand_Color hex values in a
 * BrandConfig into the Tailwind `@theme` color-scale CSS declarations
 * (`--color-{role}-{step}`). See design.md "Color_Scale_Generator" for
 * the authoritative description.
 *
 * This module exposes the pure generator functions (safe to import from
 * tests or other tooling without side effects) plus a runnable entry
 * point, guarded so it only executes when this file is run directly
 * (e.g. via `npm run prebuild` / `npm run prestart`), that writes the
 * generated `@theme` partial to `src/styles/generated/brand-theme.css`.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { BrandConfig, BrandConfigError } from '../../src/branding/brand.types';
import { DEFAULT_COLORS } from '../../src/branding/brand.defaults';
import { BRAND_CONFIG } from '../../src/branding/brand.config';

/** The 11 Tailwind steps in order. */
export const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950] as const;

/**
 * Fixed lightness deltas applied via oklch(from #hex calc(l + delta) c h).
 * step 500 is the literal hex (delta 0, emitted as the hex itself).
 */
export const LIGHTNESS_DELTA: Record<number, number> = {
  50: 0.4,
  100: 0.35,
  200: 0.3,
  300: 0.2,
  400: 0.1,
  500: 0,
  600: -0.1,
  700: -0.15,
  800: -0.2,
  900: -0.25,
  950: -0.3,
};

/** Matches a 6-digit hex color, with an optional leading '#'. Case-insensitive. */
export const HEX_COLOR_REGEX = /^#?[0-9a-fA-F]{6}$/;

/** A brand color role. */
export type BrandColorRole = 'primary' | 'secondary' | 'tertiary';

/**
 * WCAG 2.1 AA contrast target for normal-size text (1.4.3). Solid-fill
 * buttons carry normal-weight/normal-size label text, so 4.5:1 is the bar
 * rather than the 3:1 large-text/UI-component allowance.
 */
export const CONTRAST_TARGET_AA = 4.5;

/**
 * The app's dark-theme surface (`--color-gray-900`, from Tailwind's default
 * palette) expressed in OKLCH. Used as the background reference when
 * deriving the dark-theme accessible variant. Kept in sync with the
 * `html.dark body` background in src/styles.css.
 */
const DARK_SURFACE_OKLCH = { l: 0.21, c: 0.034, h: 264.665 } as const;

/** Granularity of the accessible-lightness search, in OKLCH lightness units. */
const LIGHTNESS_SEARCH_STEP = 0.005;

/** Normalize a hex input by ensuring it has a leading '#'. Assumes the value already matches HEX_COLOR_REGEX. */
function normalizeHex(hex: string): string {
  return hex.startsWith('#') ? hex : `#${hex}`;
}

/* ------------------------------------------------------------------------- *
 * Color math
 *
 * Just enough sRGB <-> OKLab/OKLCH conversion to answer one question: at what
 * OKLCH lightness does a brand hue clear a WCAG contrast threshold against a
 * given background? Conversions follow Björn Ottosson's OKLab definition;
 * sRGB output is gamut-clamped per channel, which is what a browser does when
 * `oklch()` lands outside the display gamut, so the contrast we compute here
 * matches what actually renders.
 * ------------------------------------------------------------------------- */

/** sRGB channel (0-1) -> linear-light value. */
function srgbToLinear(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** Linear-light value -> sRGB channel, clamped to the displayable [0,1] range. */
function linearToSrgb(c: number): number {
  const v = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
  return Math.min(1, Math.max(0, v));
}

/** Parse '#rrggbb' (or 'rrggbb') into sRGB channels in 0-1. */
function hexToSrgb(hex: string): [number, number, number] {
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

/** '#rrggbb' -> OKLCH { l (0-1), c, h (degrees) }. */
export function hexToOklch(hex: string): { l: number; c: number; h: number } {
  const [r, g, b] = hexToSrgb(hex).map(srgbToLinear);

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
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

/** WCAG 2.1 contrast ratio between two sRGB colors. */
export function contrastRatio(
  a: [number, number, number],
  b: [number, number, number]
): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Find the OKLCH lightness delta that brings `hex` to at least `target`
 * contrast against `background`, holding chroma and hue fixed.
 *
 * This is the "step down instead of overriding the brand color" rule: the
 * configured hue and saturation are preserved exactly; only lightness moves,
 * and only as far as it has to. Returns 0 when the color already passes, so a
 * brand color that is already dark enough is emitted unchanged.
 *
 * `direction` is 'darken' for light backgrounds (white text on a fill, or
 * brand-colored text on a white surface) and 'lighten' for dark backgrounds.
 * Both directions always converge: pushing lightness to 0 approaches black
 * (21:1 against white) and to 1 approaches white, so there is no unreachable
 * case for an AA-level target.
 */
export function findAccessibleLightnessDelta(
  hex: string,
  background: [number, number, number],
  direction: 'darken' | 'lighten',
  target: number = CONTRAST_TARGET_AA
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
      // Round to the search granularity so output is stable and readable.
      return Number((sign * magnitude).toFixed(3));
    }
  }

  // Fully saturated at the end of the ramp (pure black or white for this hue).
  return Number((sign * limit).toFixed(3));
}

/**
 * Emit the two contrast-guaranteed aliases for one role.
 *
 * `--color-{role}-accessible` clears AA against white, so it is safe both as
 * a solid fill carrying white text and as brand-colored text on a light
 * surface. `--color-{role}-accessible-dark` clears AA against the dark-theme
 * surface, for the `dark:` half of the same call site.
 *
 * Each is expressed as a lightness-only offset from the configured hex, so
 * chroma and hue are untouched and the alias tracks any future Brand_Color
 * edit automatically.
 */
export function generateAccessibleAliases(role: BrandColorRole, hex: string): string {
  const normalizedHex = normalizeHex(hex);
  const white: [number, number, number] = [1, 1, 1];
  const darkSurface = oklchToSrgb(
    DARK_SURFACE_OKLCH.l,
    DARK_SURFACE_OKLCH.c,
    DARK_SURFACE_OKLCH.h
  );

  const render = (name: string, delta: number): string => {
    if (delta === 0) {
      return `--color-${role}-${name}: ${normalizedHex};`;
    }
    const sign = delta > 0 ? '+' : '-';
    return `--color-${role}-${name}: oklch(from ${normalizedHex} calc(l ${sign} ${Math.abs(delta)}) c h);`;
  };

  return [
    render('accessible', findAccessibleLightnessDelta(normalizedHex, white, 'darken')),
    render('accessible-dark', findAccessibleLightnessDelta(normalizedHex, darkSurface, 'lighten')),
  ].join('\n');
}

/**
 * Produce the 11 CSS declarations for one role, given an already-normalized
 * (leading '#') 6-digit hex value.
 */
export function generateScale(role: BrandColorRole, hex: string): string {
  const normalizedHex = normalizeHex(hex);

  return STEPS.map((step) => {
    if (step === 500) {
      return `--color-${role}-500: ${normalizedHex};`;
    }

    const delta = LIGHTNESS_DELTA[step];
    const sign = delta >= 0 ? '+' : '-';
    const magnitude = Math.abs(delta);

    return `--color-${role}-${step}: oklch(from ${normalizedHex} calc(l ${sign} ${magnitude}) c h);`;
  }).join('\n');
}

/**
 * Validate and resolve a single role's hex value against the config,
 * falling back to the Default_Branding hex and recording an error when
 * the provided value is not a valid 6-digit hex.
 */
function resolveRoleHex(
  role: BrandColorRole,
  value: string,
  errors: BrandConfigError[]
): string {
  if (HEX_COLOR_REGEX.test(value)) {
    return normalizeHex(value);
  }

  errors.push({
    field: `colors.${role}`,
    value,
    reason: `Invalid Brand_Color hex for role "${role}": expected a 6-digit hexadecimal value (optional leading '#'), got "${value}".`,
  });

  return normalizeHex(DEFAULT_COLORS[role]);
}

/**
 * Produce the full @theme color block for all three roles (primary,
 * secondary, tertiary, in that order), validating each role's hex against
 * the Brand_Color format and falling back to Default_Branding on failure.
 */
export function generateBrandTheme(config: BrandConfig): {
  css: string;
  errors: BrandConfigError[];
} {
  const errors: BrandConfigError[] = [];
  const roles: BrandColorRole[] = ['primary', 'secondary', 'tertiary'];

  const resolved = roles.map((role) => ({
    role,
    hex: resolveRoleHex(role, config.colors[role], errors),
  }));

  // The three 11-step scales come first, as a contiguous 33-line block, then
  // the contrast-guaranteed aliases. Keeping the scales first and unbroken
  // means the scale structure stays independently addressable (the property
  // tests slice it by fixed offset).
  const scales = resolved.map(({ role, hex }) => generateScale(role, hex)).join('\n');
  const aliases = resolved
    .map(({ role, hex }) => generateAccessibleAliases(role, hex))
    .join('\n');

  return { css: `${scales}\n${aliases}`, errors };
}

/** Directory containing this script file (ESM-safe equivalent of `__dirname`). */
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

/** Path to the generated Tailwind `@theme` color-scale partial, relative to this file. */
const OUTPUT_PATH = resolve(SCRIPT_DIR, '../../src/styles/generated/brand-theme.css');

/** Wrap the generated color-scale declarations in an `@theme { ... }` block. */
function wrapInThemeBlock(css: string): string {
  const indented = css
    .split('\n')
    .map((line) => (line.length > 0 ? `    ${line}` : line))
    .join('\n');

  return `/**
 * Generated by scripts/branding/generate-brand-theme.ts. Do not edit by hand.
 *
 * Tailwind \`@theme\` color-scale declarations derived from the
 * Brand_Color values in src/branding/brand.config.ts. Regenerated
 * automatically by the \`prebuild\` / \`prestart\` npm scripts.
 *
 * Each role gets an 11-step scale plus two contrast-guaranteed aliases:
 *   --color-{role}-accessible       WCAG AA (4.5:1) against white. Use for
 *                                   solid fills with white text, and for
 *                                   brand-colored text on light surfaces.
 *   --color-{role}-accessible-dark  WCAG AA against the dark-theme surface.
 *                                   Use for the \`dark:\` half of the same
 *                                   call site.
 *
 * The aliases only move lightness — the configured hue and chroma are
 * preserved — so a brand color that is already dark enough is emitted
 * unchanged, and one that is too light is stepped down just far enough
 * rather than being replaced.
 */
@theme {
${indented}
}
`;
}

/** Run the generator against BRAND_CONFIG and write the output partial. */
function run(): void {
  const { css, errors } = generateBrandTheme(BRAND_CONFIG);

  for (const error of errors) {
    console.warn(
      `[generate-brand-theme] ${error.field}: ${error.reason}${
        error.value !== undefined ? ` (value: "${error.value}")` : ''
      }`
    );
  }

  mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
  writeFileSync(OUTPUT_PATH, wrapInThemeBlock(css), 'utf8');
  console.log(`✏️  ${OUTPUT_PATH} ← Brand_Config colors`);
}

// Runtime guard: only execute the file-writing logic when this script is
// run directly (e.g. `tsx scripts/branding/generate-brand-theme.ts`), not
// when the pure functions above are imported elsewhere (e.g. property tests).
const isMainModule = (() => {
  try {
    return resolve(process.argv[1] ?? '') === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
})();

if (isMainModule) {
  run();
}
