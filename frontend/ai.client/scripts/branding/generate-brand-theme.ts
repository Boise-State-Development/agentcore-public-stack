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
 *
 * sRGB/OKLab conversion and WCAG contrast helpers live in ./color-math.ts,
 * shared with generate-surface-theme.ts and brand-config.normalize.ts,
 * rather than being duplicated here.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { BrandConfig, BrandConfigError } from '../../src/branding/brand.types';
import { DEFAULT_COLORS, DEFAULT_SURFACES } from '../../src/branding/brand.defaults';
import { BRAND_CONFIG } from '../../src/branding/brand.config';
import { logSurfaceAcceptance, resolveSurfaces } from '../../src/branding/brand-config.normalize';
import {
  CONTRAST_TARGET_AA,
  HEX_COLOR_REGEX,
  contrastRatio,
  findAccessibleLightnessDelta,
  hexToOklch,
  normalizeHex,
  oklchToSrgb,
} from './color-math';

// Re-exported for backwards compatibility: existing specs import these
// symbols from this module.
export { HEX_COLOR_REGEX, contrastRatio, findAccessibleLightnessDelta, hexToOklch, oklchToSrgb, CONTRAST_TARGET_AA };

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

/** A brand color role. */
export type BrandColorRole = 'primary' | 'secondary' | 'tertiary';

/**
 * The app's light-theme "accessible" reference surface, in sRGB (0-1).
 * The darker of the configured `surfaces.light` and `surfaces.raised`
 * anchors, since that is the binding constraint for legible text/fills —
 * a color that clears AA against the darker of the two also clears it
 * against the lighter one. Falls back to Default_Surfaces (pure white) so
 * a clean checkout renders identically to before surfaces existed.
 */
function resolveLightSurfaceSrgb(surfaces: {
  light: string;
  raised: string;
}): [number, number, number] {
  const lightOklch = hexToOklch(surfaces.light);
  const raisedOklch = hexToOklch(surfaces.raised);
  const darker = lightOklch.l <= raisedOklch.l ? lightOklch : raisedOklch;
  return oklchToSrgb(darker.l, darker.c, darker.h);
}

/**
 * The app's dark-theme "accessible" reference surface, in sRGB (0-1),
 * derived from the configured `surfaces.dark` anchor. Falls back to
 * Default_Surfaces (Tailwind gray-900) so a clean checkout renders
 * identically to before surfaces existed. Kept in sync with the
 * `html.dark body` background, which is driven by the same
 * `surfaces.dark` value via generate-surface-theme.ts.
 */
function resolveDarkSurfaceSrgb(surfaces: { dark: string }): [number, number, number] {
  const { l, c, h } = hexToOklch(surfaces.dark);
  return oklchToSrgb(l, c, h);
}

/**
 * Emit the two contrast-guaranteed aliases for one role.
 *
 * `--color-{role}-accessible` clears AA against the resolved light surface
 * (the darker of `surfaces.light`/`surfaces.raised`), so it is safe both as
 * a solid fill carrying white text and as brand-colored text on a light
 * surface. `--color-{role}-accessible-dark` clears AA against the resolved
 * dark surface (`surfaces.dark`), for the `dark:` half of the same call
 * site.
 *
 * Each is expressed as a lightness-only offset from the configured hex, so
 * chroma and hue are untouched and the alias tracks any future Brand_Color
 * edit automatically.
 */
export function generateAccessibleAliases(
  role: BrandColorRole,
  hex: string,
  surfaces: { light: string; dark: string; raised: string } = DEFAULT_SURFACES,
): string {
  const normalizedHex = normalizeHex(hex);
  const lightSurface = resolveLightSurfaceSrgb(surfaces);
  const darkSurface = resolveDarkSurfaceSrgb(surfaces);

  const render = (name: string, delta: number): string => {
    if (delta === 0) {
      return `--color-${role}-${name}: ${normalizedHex};`;
    }
    const sign = delta > 0 ? '+' : '-';
    return `--color-${role}-${name}: oklch(from ${normalizedHex} calc(l ${sign} ${Math.abs(delta)}) c h);`;
  };

  return [
    render('accessible', findAccessibleLightnessDelta(normalizedHex, lightSurface, 'darken')),
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
 *
 * The accessible aliases are computed against `config.surfaces` (falling
 * back to Default_Surfaces per-field on invalid input), so they track the
 * app's actual light/dark backgrounds rather than a hardcoded reference.
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

  const surfaces = resolveSurfaces(config.surfaces, errors);

  // The three 11-step scales come first, as a contiguous 33-line block, then
  // the contrast-guaranteed aliases. Keeping the scales first and unbroken
  // means the scale structure stays independently addressable (the property
  // tests slice it by fixed offset).
  const scales = resolved.map(({ role, hex }) => generateScale(role, hex)).join('\n');
  const aliases = resolved
    .map(({ role, hex }) => generateAccessibleAliases(role, hex, surfaces))
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
 *   --color-{role}-accessible       WCAG AA (4.5:1) against the resolved
 *                                   light surface. Use for solid fills with
 *                                   white text, and for brand-colored text
 *                                   on light surfaces.
 *   --color-{role}-accessible-dark  WCAG AA against the resolved dark
 *                                   surface. Use for the \`dark:\` half of
 *                                   the same call site.
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

  logSurfaceAcceptance('generate-brand-theme', BRAND_CONFIG.surfaces, errors);

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
