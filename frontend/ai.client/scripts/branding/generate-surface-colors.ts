/**
 * Surface_Colors_Generator (build-time).
 *
 * Emits a resolved-hex TypeScript module (`surface-colors.ts`) for
 * Chart.js chrome (tooltip/axis/grid colors), which cannot consume CSS
 * custom properties at canvas render time. Mirrors the existing
 * `chart-colors.constants.ts` precedent (documented there as "these are
 * intentionally resolved hex values for direct Chart.js consumption")
 * and the reasoning `identity.css`'s closing comment already records for
 * why this is build-time generation rather than a `getComputedStyle`
 * bridge.
 *
 * Each named chrome color is anchored to its own *current* literal hex
 * value (not re-derived from Tailwind's OKLCH gray ramp), and shifted by
 * the same per-step OKLCH offset the surface ramp (generate-surface-
 * theme.ts) applies to that role's nominal Tailwind step. This guarantees
 * the zero-diff property byte-for-byte against today's hand-written
 * `CHART_CHROME_COLORS` (Task 7's golden test), including for roles whose
 * current literal hex is a pre-v4 (non-OKLCH-round-tripped) Tailwind gray
 * — recomputing from `TAILWIND_GRAY_RAMP` instead would silently shift
 * those by a channel or two even at default config.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { BrandConfig, BrandConfigError, BrandSurfaces } from '../../src/branding/brand.types';
import { DEFAULT_SURFACES } from '../../src/branding/brand.defaults';
import { BRAND_CONFIG } from '../../src/branding/brand.config';
import { logSurfaceAcceptance, resolveSurfaces } from '../../src/branding/brand-config.normalize';
import { hexToOklch, normalizeHex, oklchToSrgb, shortestHueDelta } from './color-math';
import { STEP_T, TAILWIND_GRAY_RAMP } from './generate-surface-theme';

/** The current hand-written chrome hex values (see chart-colors.constants.ts), used as the per-role base. */
const BASE_CHROME_HEX = {
  light: {
    titleText: '#111827', // gray-900
    bodyText: '#4b5563', // gray-600
    border: '#e5e7eb', // gray-200
    axisText: '#6b7280', // gray-500
  },
  dark: {
    background: '#1f2937', // gray-800
    bodyText: '#d1d5db', // gray-300
    border: '#374151', // gray-700
    axisText: '#9ca3af', // gray-400
  },
} as const;

/** Nominal Tailwind step each role's base hex represents, for choosing its blend position `t` (see STEP_T). */
const ROLE_STEP = {
  light: { titleText: 900, bodyText: 600, border: 200, axisText: 500 },
  dark: { background: 800, bodyText: 300, border: 700, axisText: 400 },
} as const;

/** sRGB (0-1 triple) -> nearest 6-digit hex. */
function srgbToHex([r, g, b]: [number, number, number]): string {
  const toByte = (c: number) => Math.round(Math.min(1, Math.max(0, c)) * 255);
  return `#${[r, g, b].map((c) => toByte(c).toString(16).padStart(2, '0')).join('')}`;
}

/** Shift `baseHex` by the OKLCH offset blended at position `t` between the light/dark deltas. */
function shiftHex(
  baseHex: string,
  t: number,
  deltaLight: { l: number; c: number; h: number },
  deltaDark: { l: number; c: number; h: number },
): string {
  const base = hexToOklch(baseHex);
  const l = base.l + (t * deltaLight.l + (1 - t) * deltaDark.l);
  const c = Math.max(0, base.c + (t * deltaLight.c + (1 - t) * deltaDark.c));
  const h = ((base.h + (t * deltaLight.h + (1 - t) * deltaDark.h)) % 360 + 360) % 360;
  return srgbToHex(oklchToSrgb(l, c, h));
}

/** The resolved chart chrome colors this generator produces, mirroring CHART_CHROME_COLORS's shape. */
export interface ResolvedSurfaceColors {
  light: {
    background: string;
    titleText: string;
    bodyText: string;
    border: string;
    axisText: string;
    gridLine: string;
  };
  dark: {
    background: string;
    titleText: string;
    bodyText: string;
    border: string;
    axisText: string;
    gridLine: string;
  };
}

/**
 * Resolve the chrome color set for the given (already-validated) surfaces.
 * At `DEFAULT_SURFACES` every value is `BASE_CHROME_HEX`/the existing
 * literals verbatim (zero-diff fast path), exactly matching today's
 * hand-written `CHART_CHROME_COLORS`.
 */
export function resolveSurfaceColors(surfaces: BrandSurfaces): ResolvedSurfaceColors {
  const lightHex = normalizeHex(surfaces.light);
  const darkHex = normalizeHex(surfaces.dark);
  const raisedHex = normalizeHex(surfaces.raised);

  const isDefault = lightHex === normalizeHex(DEFAULT_SURFACES.light) && darkHex === normalizeHex(DEFAULT_SURFACES.dark);

  if (isDefault) {
    return {
      light: {
        background: raisedHex,
        titleText: BASE_CHROME_HEX.light.titleText,
        bodyText: BASE_CHROME_HEX.light.bodyText,
        border: BASE_CHROME_HEX.light.border,
        axisText: BASE_CHROME_HEX.light.axisText,
        gridLine: 'rgba(0, 0, 0, 0.1)',
      },
      dark: {
        background: BASE_CHROME_HEX.dark.background,
        titleText: raisedHex, // dark-mode tooltip text: pure white, tracks --color-white/surfaces.raised
        bodyText: BASE_CHROME_HEX.dark.bodyText,
        border: BASE_CHROME_HEX.dark.border,
        axisText: BASE_CHROME_HEX.dark.axisText,
        gridLine: 'rgba(255, 255, 255, 0.1)',
      },
    };
  }

  const configuredLight = hexToOklch(lightHex);
  const configuredDark = hexToOklch(darkHex);
  const deltaLight = {
    l: configuredLight.l - TAILWIND_GRAY_RAMP[50].l,
    c: configuredLight.c - TAILWIND_GRAY_RAMP[50].c,
    h: shortestHueDelta(TAILWIND_GRAY_RAMP[50].h, configuredLight.h),
  };
  const deltaDark = {
    l: configuredDark.l - TAILWIND_GRAY_RAMP[900].l,
    c: configuredDark.c - TAILWIND_GRAY_RAMP[900].c,
    h: shortestHueDelta(TAILWIND_GRAY_RAMP[900].h, configuredDark.h),
  };

  const shift = (baseHex: string, step: number) => shiftHex(baseHex, STEP_T[step], deltaLight, deltaDark);

  return {
    light: {
      background: raisedHex,
      titleText: shift(BASE_CHROME_HEX.light.titleText, ROLE_STEP.light.titleText),
      bodyText: shift(BASE_CHROME_HEX.light.bodyText, ROLE_STEP.light.bodyText),
      border: shift(BASE_CHROME_HEX.light.border, ROLE_STEP.light.border),
      axisText: shift(BASE_CHROME_HEX.light.axisText, ROLE_STEP.light.axisText),
      gridLine: 'rgba(0, 0, 0, 0.1)',
    },
    dark: {
      background: shift(BASE_CHROME_HEX.dark.background, ROLE_STEP.dark.background),
      titleText: raisedHex,
      bodyText: shift(BASE_CHROME_HEX.dark.bodyText, ROLE_STEP.dark.bodyText),
      border: shift(BASE_CHROME_HEX.dark.border, ROLE_STEP.dark.border),
      axisText: shift(BASE_CHROME_HEX.dark.axisText, ROLE_STEP.dark.axisText),
      gridLine: 'rgba(255, 255, 255, 0.1)',
    },
  };
}

/**
 * Validate `config.surfaces` (falling back to Default_Surfaces per-field
 * on invalid input, recording a `BrandConfigError`, never throwing) and
 * resolve the chart chrome color set.
 */
export function generateSurfaceColors(config: BrandConfig): { colors: ResolvedSurfaceColors; errors: BrandConfigError[] } {
  const errors: BrandConfigError[] = [];
  const surfaces = resolveSurfaces(config.surfaces, errors);
  return { colors: resolveSurfaceColors(surfaces), errors };
}

/** Render the resolved colors as a TypeScript module matching CHART_CHROME_COLORS's shape. */
function renderModule(colors: ResolvedSurfaceColors): string {
  const renderMode = (mode: ResolvedSurfaceColors['light']): string =>
    `{
    background: '${mode.background}' as const,
    titleText: '${mode.titleText}' as const,
    bodyText: '${mode.bodyText}' as const,
    border: '${mode.border}' as const,
    axisText: '${mode.axisText}' as const,
    gridLine: '${mode.gridLine}' as const,
  }`;

  return `/**
 * Generated by scripts/branding/generate-surface-colors.ts. Do not edit by
 * hand.
 *
 * Resolved chart-chrome colors derived from the Brand_Surface values in
 * src/branding/brand.config.ts, for direct Chart.js consumption (Chart.js
 * needs a resolved string, not a CSS custom property). Regenerated
 * automatically by the \`prebuild\` / \`prestart\` npm scripts. Consumed by
 * chart-colors.constants.ts's \`CHART_CHROME_COLORS\`.
 */
export const GENERATED_SURFACE_CHROME_COLORS = {
  light: ${renderMode(colors.light)},
  dark: ${renderMode(colors.dark)},
} as const;
`;
}

/** Directory containing this script file (ESM-safe equivalent of `__dirname`). */
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

/** Path to the generated surface-colors TS module, relative to this file. */
const OUTPUT_PATH = resolve(SCRIPT_DIR, '../../src/styles/generated/surface-colors.ts');

/** Run the generator against BRAND_CONFIG and write the output module. */
function run(): void {
  const { colors, errors } = generateSurfaceColors(BRAND_CONFIG);

  for (const error of errors) {
    console.warn(
      `[generate-surface-colors] ${error.field}: ${error.reason}${
        error.value !== undefined ? ` (value: "${error.value}")` : ''
      }`,
    );
  }

  logSurfaceAcceptance('generate-surface-colors', BRAND_CONFIG.surfaces, errors);

  mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
  writeFileSync(OUTPUT_PATH, renderModule(colors), 'utf8');
  console.log(`✏️  ${OUTPUT_PATH} ← Brand_Config surfaces`);
}

// Runtime guard: only execute the file-writing logic when this script is
// run directly, not when the pure functions above are imported elsewhere.
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
