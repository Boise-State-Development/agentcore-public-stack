// surface-theme-golden.spec.ts
//
// Golden regression test for the Surface_Ramp_Generator, mirroring
// brand-theme-golden.spec.ts. Runs generateSurfaceTheme with the live
// BRAND_CONFIG and asserts the emitted declarations are character-for-
// character identical to the committed
// src/styles/generated/surface-theme.css — i.e. that the committed
// artifact isn't stale relative to the current brand.config.ts, whatever
// `surfaces` currently holds. A second case pins the Default_Surfaces
// zero-diff property directly (independent of whatever BRAND_CONFIG's
// live `surfaces` currently is), since that guarantee is not observable
// from the golden file alone once `surfaces` is a non-default test
// value.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { generateSurfaceTheme } from '../../scripts/branding/generate-surface-theme';
import { BRAND_CONFIG } from './brand.config';
import { DEFAULT_SURFACES } from './brand.defaults';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));
const GOLDEN_CSS_PATH = resolve(SPEC_DIR, '../styles/generated/surface-theme.css');

/** Mirrors brand-theme-golden.spec.ts's extractThemeDeclarations. */
function extractThemeDeclarations(fileContents: string): string {
  const openIndex = fileContents.indexOf('@theme {');
  const closeIndex = fileContents.lastIndexOf('}');
  if (openIndex === -1 || closeIndex === -1) {
    throw new Error('surface-theme.css does not contain a recognizable @theme { ... } block');
  }

  const blockStart = openIndex + '@theme {'.length;
  const inner = fileContents.slice(blockStart, closeIndex);

  return inner
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => line.replace(/^ {4}/, ''))
    .join('\n');
}

describe('Surface_Ramp_Generator golden regression (Default_Surfaces)', () => {
  it('matches the committed @theme declarations character-for-character', () => {
    const goldenFileContents = readFileSync(GOLDEN_CSS_PATH, 'utf8');
    const expectedDeclarations = extractThemeDeclarations(goldenFileContents);

    const { css, errors } = generateSurfaceTheme(BRAND_CONFIG);

    expect(css).toBe(expectedDeclarations);
    // The live BRAND_CONFIG's surfaces may be in- or out-of-band (and, as
    // of the deliberately loud pink/red test values, may also trip the
    // step-500 contrast clamp on the *light* side too, not just dark), so
    // this permits any of: the documented step-500 opposing-constraint
    // warning, a step-500 light-side clamp, or a surfaces.* band
    // rejection — but never a hex-format rejection unrelated to surfaces.
    expect(
      errors.every((e) => e.field === 'surfaces.gray-500' || e.field.startsWith('surfaces.')),
    ).toBe(true);
  });

  it('matches TAILWIND_GRAY_RAMP/--color-white: #ffffff for an explicit Default_Surfaces config, with no band-rejection errors', () => {
    // This asserts against generateSurfaceTheme's own output for an
    // explicit Default_Surfaces config, not against the committed golden
    // file — the committed file reflects whatever BRAND_CONFIG.surfaces
    // currently holds (which may be a deliberately non-default test
    // value), so it is not a valid Default_Surfaces reference on its own.
    const { css, errors } = generateSurfaceTheme({ ...BRAND_CONFIG, surfaces: { ...DEFAULT_SURFACES } });

    // At Default_Surfaces specifically, the only permissible entry is the
    // documented step-500 opposing-constraint warning — never a
    // hex-validation/band rejection error.
    expect(errors.every((e) => e.field === 'surfaces.gray-500')).toBe(true);
    expect(css).toContain('--color-white: #ffffff;');
    expect(css).toContain('--color-gray-50: oklch(98.5% 0.002 247.839);');
    expect(css).toContain('--color-gray-900: oklch(21% 0.034 264.665);');
  });
});
