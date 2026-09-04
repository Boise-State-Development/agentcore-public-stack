/**
 * Surface_Ramp_Generator (build-time).
 *
 * Pure transformation from the three Brand_Surface hex anchors
 * (`surfaces.light`, `surfaces.dark`, `surfaces.raised`) into a
 * brand-configurable neutral ramp: an OKLCH-delta remap of Tailwind's own
 * `--color-gray-*` scale, plus a `--color-white` override driven by
 * `surfaces.raised`.
 *
 * This is a separate generator/output file from generate-brand-theme.ts
 * (not an extension of it) because:
 *   - brand-theme-golden.spec.ts's `extractThemeDeclarations` locates the
 *     closing brace of brand-theme.css with `lastIndexOf('}')`, so a
 *     second `@theme` block in that file would break it.
 *   - generate-brand-theme.spec.ts's per-role isolation property test
 *     slices the generator's output by a fixed 11-line-per-role offset,
 *     which a surfaces block appended after would also break.
 *
 * ## The delta remap
 *
 * Tailwind's gray ramp is not linear in lightness (98.5% at step 50 down
 * to 13% at step 950, with a large jump between steps 300 and 400), so
 * naively interpolating between two configured anchors would visibly
 * flatten or distort the ramp's character even at default values. Instead,
 * each of the 11 steps gets an OKLCH *offset* added to its own literal
 * Tailwind value:
 *
 *   offset(step) = t · deltaLight + (1 - t) · deltaDark
 *
 * where `t` is fixed per step (not user-configurable) from where that
 * step's literal Tailwind lightness sits between the dark anchor (t = 0,
 * pinned to step 900) and the light anchor (t = 1, pinned to step 50).
 * Step 950 extrapolates past t = 0 (it is already darker than 900), which
 * is exactly what keeps it darker than 900 after remapping.
 *
 * `deltaLight` / `deltaDark` are computed against `TAILWIND_GRAY_RAMP[50]`
 * / `[900]`'s own literal numeric OKLCH triples (not a hex round-trip of
 * them), so the algebra is exact for *any* in-band anchor: `final(50) =
 * TAILWIND_GRAY_RAMP[50] + (hexToOklch(light) - TAILWIND_GRAY_RAMP[50]) =
 * hexToOklch(light)` precisely, and likewise `final(900) = hexToOklch(dark)`
 * — this is the "endpoints land exactly" property.
 *
 * `DEFAULT_SURFACES.light` / `.dark` are the hex round-trips of Tailwind
 * gray-50 / gray-900, so they are extremely close to (but not bit-
 * identical to) the literal ramp due to 8-bit hex quantization — plugging
 * them through the general formula above would perturb every step by a
 * few ten-thousandths, which is visually nothing but would break a
 * character-for-character text comparison. `generateSurfaceRamp` therefore
 * special-cases surfaces that are string-identical to `DEFAULT_SURFACES`
 * and emits `TAILWIND_GRAY_RAMP`'s literal declaration text verbatim in
 * that case, bypassing the numeric path entirely — this is the "zero-diff
 * at defaults" property.
 *
 * Hue offsets are computed via the shortest arc (`shortestHueDelta`) so
 * anchors that straddle the 0/360 boundary interpolate the short way
 * around rather than wrapping the long way.
 *
 * Chroma is clamped at zero (a configured anchor's delta could otherwise
 * push a step's chroma negative, which is not a representable color).
 *
 * `surfaces.raised` does not participate in the gray-*-ramp delta remap —
 * it only drives `--color-white` (see `generateWhiteOverride`), per the
 * plan's Task 6 split.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { BrandConfig, BrandConfigError, BrandSurfaces } from '../../src/branding/brand.types';
import { DEFAULT_SURFACES } from '../../src/branding/brand.defaults';
import { BRAND_CONFIG } from '../../src/branding/brand.config';
import { logSurfaceAcceptance, resolveSurfaces } from '../../src/branding/brand-config.normalize';
import {
  contrastRatio,
  hexToOklch,
  normalizeHex,
  oklchToSrgb,
  shortestHueDelta,
} from './color-math';

/** The 11 Tailwind gray steps in order (same STEPS as generate-brand-theme.ts). */
export const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950] as const;

/**
 * Tailwind's `--color-gray-*` scale, copied verbatim (as both the literal
 * emitted string and the parsed numeric triple) from
 * `node_modules/tailwindcss/theme.css`. Kept in `oklch()` rather than hex,
 * matching the discipline `state.css` and `identity.css` document, so
 * there is no gamut clamping or wide-gamut shift versus Tailwind's own
 * palette.
 */
export const TAILWIND_GRAY_RAMP: Record<number, { l: number; c: number; h: number; text: string }> = {
  50: { l: 0.985, c: 0.002, h: 247.839, text: '98.5% 0.002 247.839' },
  100: { l: 0.967, c: 0.003, h: 264.542, text: '96.7% 0.003 264.542' },
  200: { l: 0.928, c: 0.006, h: 264.531, text: '92.8% 0.006 264.531' },
  300: { l: 0.872, c: 0.01, h: 258.338, text: '87.2% 0.01 258.338' },
  400: { l: 0.707, c: 0.022, h: 261.325, text: '70.7% 0.022 261.325' },
  500: { l: 0.551, c: 0.027, h: 264.364, text: '55.1% 0.027 264.364' },
  600: { l: 0.446, c: 0.03, h: 256.802, text: '44.6% 0.03 256.802' },
  700: { l: 0.373, c: 0.034, h: 259.733, text: '37.3% 0.034 259.733' },
  800: { l: 0.278, c: 0.033, h: 256.848, text: '27.8% 0.033 256.848' },
  900: { l: 0.21, c: 0.034, h: 264.665, text: '21% 0.034 264.665' },
  950: { l: 0.13, c: 0.028, h: 261.692, text: '13% 0.028 261.692' },
};

/** `t` position of each step between the dark anchor (t=0, step 900) and the light anchor (t=1, step 50), fixed by the literal ramp's own lightness values — never user-configurable. */
export const STEP_T: Record<number, number> = (() => {
  const l50 = TAILWIND_GRAY_RAMP[50].l;
  const l900 = TAILWIND_GRAY_RAMP[900].l;
  const span = l50 - l900;
  const result: Record<number, number> = {};
  for (const step of STEPS) {
    result[step] = (TAILWIND_GRAY_RAMP[step].l - l900) / span;
  }
  return result;
})();

/** An OKLCH offset (delta), as applied additively to a base ramp step. */
interface OklchOffset {
  l: number;
  c: number;
  h: number;
}

/** Compute the OKLCH delta of `configured` versus a ramp anchor step's own literal triple, hue on the shortest arc. */
function deltaAgainstAnchorStep(
  configured: { l: number; c: number; h: number },
  anchorStep: number,
): OklchOffset {
  const base = TAILWIND_GRAY_RAMP[anchorStep];
  return {
    l: configured.l - base.l,
    c: configured.c - base.c,
    h: shortestHueDelta(base.h, configured.h),
  };
}

/** Format a lightness fraction (0-1) as a Tailwind-style percentage, trimming trailing zeros. */
function formatPercent(l: number): string {
  const pct = l * 100;
  const rounded = Math.round(pct * 1000) / 1000;
  return `${trimTrailingZeros(rounded)}%`;
}

/** Format a chroma or hue number, trimming trailing zeros, matching Tailwind's own literal style. */
function formatNumber(n: number): string {
  const rounded = Math.round(n * 1000) / 1000;
  return trimTrailingZeros(rounded);
}

function trimTrailingZeros(n: number): string {
  // Avoid "-0" and unnecessary trailing ".000"/".0" artifacts.
  const normalized = n === 0 ? 0 : n;
  return String(normalized);
}

/**
 * Compute the remapped OKLCH ramp for the given surfaces (light/dark only —
 * `raised` does not participate; see the module header). Returns, for each
 * step, both the literal emit-ready declaration line and the resolved
 * numeric OKLCH triple (the latter is what Task 3's contrast clamp reads
 * and rewrites).
 */
export function generateSurfaceRamp(
  surfaces: Pick<BrandSurfaces, 'light' | 'dark'>,
): Record<number, { l: number; c: number; h: number; line: string }> {
  const lightHex = normalizeHex(surfaces.light);
  const darkHex = normalizeHex(surfaces.dark);

  // Zero-diff fast path: string-identical to DEFAULT_SURFACES means every
  // step's declaration is TAILWIND_GRAY_RAMP verbatim — no float
  // arithmetic involved, so there is no risk of a hex-round-trip
  // quantization difference perturbing the committed golden output.
  if (lightHex === normalizeHex(DEFAULT_SURFACES.light) && darkHex === normalizeHex(DEFAULT_SURFACES.dark)) {
    const result: Record<number, { l: number; c: number; h: number; line: string }> = {};
    for (const step of STEPS) {
      const base = TAILWIND_GRAY_RAMP[step];
      result[step] = { l: base.l, c: base.c, h: base.h, line: `--color-gray-${step}: oklch(${base.text});` };
    }
    return result;
  }

  const configuredLight = hexToOklch(lightHex);
  const configuredDark = hexToOklch(darkHex);

  // Deltas are computed against the anchor steps' own literal numeric
  // triples (not a hex round-trip of them), so the remap lands the
  // configured anchor exactly at t=1 (step 50) and t=0 (step 900) — see
  // the module header's "endpoints land exactly" note.
  const deltaLight = deltaAgainstAnchorStep(configuredLight, 50);
  const deltaDark = deltaAgainstAnchorStep(configuredDark, 900);

  const result: Record<number, { l: number; c: number; h: number; line: string }> = {};

  for (const step of STEPS) {
    const base = TAILWIND_GRAY_RAMP[step];
    const t = STEP_T[step];

    const offset: OklchOffset = {
      l: t * deltaLight.l + (1 - t) * deltaDark.l,
      c: t * deltaLight.c + (1 - t) * deltaDark.c,
      h: t * deltaLight.h + (1 - t) * deltaDark.h,
    };

    const l = base.l + offset.l;
    const c = Math.max(0, base.c + offset.c);
    const h = ((base.h + offset.h) % 360 + 360) % 360;

    result[step] = {
      l,
      c,
      h,
      line: `--color-gray-${step}: oklch(${formatPercent(l)} ${formatNumber(c)} ${formatNumber(h)});`,
    };
  }

  return result;
}

/**
 * Light-mode text steps that sit on a light surface (`text-gray-{step}`
 * call sites): darkened as needed to clear AA against the *darker* of
 * `surfaces.light` and `surfaces.raised` (the binding constraint — a step
 * that clears contrast against the darker surface also clears it against
 * the lighter one).
 */
const LIGHT_TEXT_STEPS = [500, 600, 700, 900] as const;

/**
 * Dark-mode text steps that sit on the dark surface
 * (`dark:text-gray-{step}` call sites): lightened as needed to clear AA
 * against `surfaces.dark`.
 */
const DARK_TEXT_STEPS = [200, 300, 400] as const;

/**
 * Step 500 is consumed as *both* `text-gray-500` (860 uses, light mode)
 * and `dark:text-gray-500` (209 uses, dark mode) — the same CSS variable
 * under two opposing constraints. It is clamped for the light-mode
 * constraint (it's in LIGHT_TEXT_STEPS above); this constant marks it for
 * a dark-mode contrast *check* that only warns, never clamps, since
 * clamping it lighter to satisfy dark mode would un-satisfy the light-mode
 * clamp already applied.
 */
const OPPOSING_CONSTRAINT_STEP = 500;

/** Granularity of the accessible-lightness search, in OKLCH lightness units (mirrors color-math.ts's LIGHTNESS_SEARCH_STEP). */
const LIGHTNESS_SEARCH_STEP = 0.0005;

/**
 * Find the smallest lightness (holding chroma/hue fixed) that clears
 * `target` contrast against `background`, searching directly in OKLCH
 * float space rather than round-tripping through an 8-bit hex quantization
 * (which is what color-math.ts's findAccessibleLightnessDelta does, and
 * which can leave the result a hair under target for some inputs). A
 * finer step (0.0005 vs. 0.005) than the brand-color generator's search
 * because this clamp's correctness is asserted by an exact property test,
 * not just visually.
 */
function searchAccessibleLightness(
  l: number,
  c: number,
  h: number,
  background: [number, number, number],
  direction: 'darken' | 'lighten',
  target = 4.5,
): number {
  const sign = direction === 'darken' ? -1 : 1;
  const limit = direction === 'darken' ? l : 1 - l;

  for (let magnitude = LIGHTNESS_SEARCH_STEP; magnitude <= limit; magnitude += LIGHTNESS_SEARCH_STEP) {
    const candidate = Math.min(1, Math.max(0, l + sign * magnitude));
    if (contrastRatio(oklchToSrgb(candidate, c, h), background) >= target) {
      return candidate;
    }
  }

  return direction === 'darken' ? 0 : 1;
}

/** Re-render a ramp step's line after its lightness has been clamped, preserving chroma/hue. */
function renderStepLine(step: number, l: number, c: number, h: number): string {
  return `--color-gray-${step}: oklch(${formatPercent(l)} ${formatNumber(c)} ${formatNumber(h)});`;
}

/**
 * Apply the WCAG AA contrast clamp (Task 3) to an already-remapped ramp,
 * in place conceptually (returns a new ramp object; does not mutate the
 * input). For each `LIGHT_TEXT_STEPS` / `DARK_TEXT_STEPS` entry that fails
 * 4.5:1 against its bound surface, nudges lightness only (hue/chroma
 * untouched) just far enough to clear the target, and records an
 * informational `BrandConfigError` naming the step and measured ratio.
 * Step 500's dark-mode pairing is checked and reported as a warning
 * without being clamped (see `OPPOSING_CONSTRAINT_STEP`).
 *
 * Inert when every step already clears its target — including, by
 * construction, at Default_Surfaces (Task 2's zero-diff ramp starts from
 * Tailwind's own well-contrasted defaults).
 */
export function applyContrastClamp(
  ramp: Record<number, { l: number; c: number; h: number; line: string }>,
  surfaces: BrandSurfaces,
  errors: BrandConfigError[],
): Record<number, { l: number; c: number; h: number; line: string }> {
  const result: Record<number, { l: number; c: number; h: number; line: string }> = { ...ramp };

  const lightSurfaceOklch = hexToOklch(surfaces.light);
  const raisedSurfaceOklch = hexToOklch(surfaces.raised);
  const lightBgOklch = lightSurfaceOklch.l <= raisedSurfaceOklch.l ? lightSurfaceOklch : raisedSurfaceOklch;
  const lightBg = oklchToSrgb(lightBgOklch.l, lightBgOklch.c, lightBgOklch.h);

  const darkSurfaceOklch = hexToOklch(surfaces.dark);
  const darkBg = oklchToSrgb(darkSurfaceOklch.l, darkSurfaceOklch.c, darkSurfaceOklch.h);

  const clamp = (
    step: number,
    background: [number, number, number],
    direction: 'darken' | 'lighten',
    backgroundLabel: string,
  ): void => {
    const entry = result[step];
    const srgb = oklchToSrgb(entry.l, entry.c, entry.h);
    const ratio = contrastRatio(srgb, background);
    if (ratio >= 4.5) return;

    // Search directly in OKLCH float space (not via a hex round-trip,
    // which quantizes to 8-bit-per-channel and can leave the result a
    // hair under target) for the smallest lightness step, in the given
    // direction, that clears 4.5:1.
    const newL = searchAccessibleLightness(entry.l, entry.c, entry.h, background, direction);

    result[step] = {
      l: newL,
      c: entry.c,
      h: entry.h,
      line: renderStepLine(step, newL, entry.c, entry.h),
    };

    errors.push({
      field: `surfaces.gray-${step}`,
      value: undefined,
      reason: `--color-gray-${step} was clamped darker/lighter to clear WCAG AA (4.5:1) against ${backgroundLabel} (measured ratio: ${ratio.toFixed(2)}:1)`,
    });
  };

  for (const step of LIGHT_TEXT_STEPS) {
    clamp(step, lightBg, 'darken', 'the resolved light surface');
  }

  for (const step of DARK_TEXT_STEPS) {
    clamp(step, darkBg, 'lighten', 'surfaces.dark');
  }

  // Step 500's dark-mode pairing (`dark:text-gray-500`) is checked but never
  // clamped: it would conflict with the light-mode clamp already applied
  // to the same variable above.
  const step500 = result[OPPOSING_CONSTRAINT_STEP];
  const step500Srgb = oklchToSrgb(step500.l, step500.c, step500.h);
  const step500DarkRatio = contrastRatio(step500Srgb, darkBg);
  if (step500DarkRatio < 4.5) {
    errors.push({
      field: `surfaces.gray-${OPPOSING_CONSTRAINT_STEP}`,
      value: undefined,
      reason:
        `dark:text-gray-${OPPOSING_CONSTRAINT_STEP} does not clear WCAG AA (4.5:1) against surfaces.dark ` +
        `(measured ratio: ${step500DarkRatio.toFixed(2)}:1). This step is shared with the light-mode ` +
        `text-gray-${OPPOSING_CONSTRAINT_STEP} constraint and is clamped for that constraint instead — ` +
        `this is a known limitation, not a bug.`,
    });
  }

  return result;
}

/**
 * The `--color-white` declaration line, driven by `surfaces.raised`
 * (Task 6). Emitted as the literal configured hex — `raised` is a flat
 * anchor, not a scale, so there is no delta remap to apply.
 */
function generateWhiteOverride(raisedHex: string): string {
  return `--color-white: ${raisedHex};`;
}

/**
 * Produce the full surface `@theme` declaration block (11 `--color-gray-*`
 * lines plus `--color-white`), validating each anchor against the
 * Brand_Surface hex format and falling back to Default_Surfaces on
 * per-field failure — never throwing.
 */
export function generateSurfaceTheme(config: BrandConfig): { css: string; errors: BrandConfigError[] } {
  const errors: BrandConfigError[] = [];
  const { light, dark, raised } = resolveSurfaces(config.surfaces, errors);

  const remapped = generateSurfaceRamp({ light, dark });
  const ramp = applyContrastClamp(remapped, { light, dark, raised }, errors);
  const rampLines = STEPS.map((step) => ramp[step].line).join('\n');
  const whiteLine = generateWhiteOverride(raised);

  return { css: `${rampLines}\n${whiteLine}`, errors };
}

/** Directory containing this script file (ESM-safe equivalent of `__dirname`). */
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));

/** Path to the generated Tailwind `@theme` surface partial, relative to this file. */
const OUTPUT_PATH = resolve(SCRIPT_DIR, '../../src/styles/generated/surface-theme.css');

/** Wrap the generated surface declarations in an `@theme { ... }` block. */
function wrapInThemeBlock(css: string): string {
  const indented = css
    .split('\n')
    .map((line) => (line.length > 0 ? `    ${line}` : line))
    .join('\n');

  return `/**
 * Generated by scripts/branding/generate-surface-theme.ts. Do not edit by
 * hand.
 *
 * Tailwind \`@theme\` neutral-ramp declarations derived from the
 * Brand_Surface values (\`surfaces.light\`, \`surfaces.dark\`,
 * \`surfaces.raised\`) in src/branding/brand.config.ts. Regenerated
 * automatically by the \`prebuild\` / \`prestart\` npm scripts.
 *
 * Overrides Tailwind's default \`--color-gray-*\` scale and
 * \`--color-white\`, so every \`bg-gray-*\`, \`border-gray-*\`,
 * \`text-gray-*\`, and \`bg-white\` utility (and every \`var(--color-gray-*)\`
 * reference in styles.css/component stylesheets) picks up the configured
 * surfaces automatically — not just \`body\`.
 *
 * At Default_Surfaces (surfaces.light = #f9fafb, surfaces.dark = #101828,
 * surfaces.raised = #ffffff) every declaration below is emitted verbatim
 * from Tailwind's own published gray scale — see
 * generate-surface-theme.spec.ts's zero-diff property test.
 */
@theme {
${indented}
}
`;
}

/** Run the generator against BRAND_CONFIG and write the output partial. */
function run(): void {
  const { css, errors } = generateSurfaceTheme(BRAND_CONFIG);

  for (const error of errors) {
    console.warn(
      `[generate-surface-theme] ${error.field}: ${error.reason}${
        error.value !== undefined ? ` (value: "${error.value}")` : ''
      }`,
    );
  }

  // Confirm what each anchor actually resolved to, so acceptance is just
  // as visible as rejection — a forker staring at a blank console after
  // editing `surfaces` has no way to tell "accepted, rendering now" from
  // "silently still running with a stale build" otherwise.
  logSurfaceAcceptance('generate-surface-theme', BRAND_CONFIG.surfaces, errors);

  mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
  writeFileSync(OUTPUT_PATH, wrapInThemeBlock(css), 'utf8');
  console.log(`✏️  ${OUTPUT_PATH} ← Brand_Config surfaces`);
}

// Runtime guard: only execute the file-writing logic when this script is
// run directly (e.g. `tsx scripts/branding/generate-surface-theme.ts`), not
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
