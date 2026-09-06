// brand-theme-golden.spec.ts
//
// Golden regression test for the Color_Scale_Generator (Requirement 7.2).
// Runs generateBrandTheme with the Default_Branding hexes (via BRAND_CONFIG,
// which is currently populated with Default_Branding values — see
// brand.config.ts) and asserts the emitted color declarations are
// character-for-character identical to the currently committed
// src/styles/generated/brand-theme.css (produced by the generator's file-
// writing entry point, task 3.1). This is the guard that centralizing
// colors into Brand_Config does not change any pixel under default
// branding. See design.md "Snapshot / golden regression tests".
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { generateBrandTheme } from '../../scripts/branding/generate-brand-theme';
import { BRAND_CONFIG } from './brand.config';
import { DEFAULT_SURFACES } from './brand.defaults';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));
const GOLDEN_CSS_PATH = resolve(SPEC_DIR, '../styles/generated/brand-theme.css');

/**
 * Extract just the declaration lines from the committed brand-theme.css:
 * the lines inside the `@theme { ... }` block, with the file's 4-space
 * indentation stripped, matching the un-indented `css` string shape
 * returned by generateBrandTheme(). (wrapInThemeBlock in
 * generate-brand-theme.ts is not exported, so we parse the committed
 * file directly rather than re-deriving the wrapping logic.)
 */
function extractThemeDeclarations(fileContents: string): string {
  const openIndex = fileContents.indexOf('@theme {');
  const closeIndex = fileContents.lastIndexOf('}');
  if (openIndex === -1 || closeIndex === -1) {
    throw new Error('brand-theme.css does not contain a recognizable @theme { ... } block');
  }

  const blockStart = openIndex + '@theme {'.length;
  const inner = fileContents.slice(blockStart, closeIndex);

  return inner
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => line.replace(/^ {4}/, ''))
    .join('\n');
}

describe('Color_Scale_Generator golden regression (Default_Branding)', () => {
  it('matches the committed @theme color declarations character-for-character', () => {
    const goldenFileContents = readFileSync(GOLDEN_CSS_PATH, 'utf8');
    const expectedDeclarations = extractThemeDeclarations(goldenFileContents);

    const { css, errors } = generateBrandTheme(BRAND_CONFIG);

    expect(css).toBe(expectedDeclarations);
    // Once generateBrandTheme applies the surfaces bands (cluster B/C), an
    // out-of-band live BRAND_CONFIG.surfaces value records a
    // surfaces.* rejection here too — that is a correct, expected
    // consequence of Requirement 2.5, not a regression. What this spec
    // actually guards is that the *colors* path stays clean.
    expect(errors.some((e) => e.field.startsWith('colors.'))).toBe(false);
  });

  it('records no errors at all for an explicit Default_Surfaces config', () => {
    const { errors } = generateBrandTheme({ ...BRAND_CONFIG, surfaces: { ...DEFAULT_SURFACES } });
    expect(errors).toEqual([]);
  });
});
