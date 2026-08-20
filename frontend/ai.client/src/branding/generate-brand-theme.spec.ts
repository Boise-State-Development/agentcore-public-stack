// generate-brand-theme.spec.ts
//
// Property-based tests for the Color_Scale_Generator (scripts/branding/generate-brand-theme.ts).
// Covers Property 1 (color scale structure) and Property 2 (lightness derivation holds
// chroma and hue). See design.md "Correctness Properties" for the authoritative property
// text and requirements.md 5.2-5.4 for the acceptance criteria being validated.
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  generateScale,
  generateBrandTheme,
  STEPS,
  LIGHTNESS_DELTA,
  HEX_COLOR_REGEX,
  type BrandColorRole,
} from '../../scripts/branding/generate-brand-theme';
import type { BrandConfig } from './brand.types';
import { DEFAULT_COLORS } from './brand.defaults';

/**
 * Arbitrary for a valid 6-digit hex color input: 6 hex digits, an optional
 * leading '#', and mixed case — matching the generator's accepted input
 * shape (HEX_COLOR_REGEX = /^#?[0-9a-fA-F]{6}$/).
 */
const arbValidHex = fc
  .tuple(
    fc.array(fc.constantFrom(..."0123456789abcdefABCDEF".split('')), {
      minLength: 6,
      maxLength: 6,
    }),
    fc.boolean(), // whether to prepend '#'
  )
  .map(([digits, withHash]) => {
    const hex = digits.join('');
    return withHash ? `#${hex}` : hex;
  });

const arbRole: fc.Arbitrary<BrandColorRole> = fc.constantFrom('primary', 'secondary', 'tertiary');

/** Normalize the way the generator does: ensure a leading '#', preserving case. */
function normalizeHex(hex: string): string {
  return hex.startsWith('#') ? hex : `#${hex}`;
}

describe('generateScale', () => {
  // Feature: branding-customization, Property 1: Color scale structure
  // Validates: Requirements 5.2, 5.4
  it('produces exactly 11 declarations named --color-{role}-{step} in order, with step-500 the literal input hex', () => {
    fc.assert(
      fc.property(arbRole, arbValidHex, (role, hex) => {
        const css = generateScale(role, hex);
        const lines = css.split('\n');

        expect(lines).toHaveLength(11);

        lines.forEach((line, index) => {
          const step = STEPS[index];
          expect(line.startsWith(`--color-${role}-${step}: `)).toBe(true);
          expect(line.endsWith(';')).toBe(true);
        });

        // Step 500 is the literal input hex, unchanged (accounting for the
        // leading '#' normalization the generator applies).
        const step500Line = lines[STEPS.indexOf(500)];
        expect(step500Line).toBe(`--color-${role}-500: ${normalizeHex(hex)};`);
      }),
      { numRuns: 100 }
    );
  });

  // Feature: branding-customization, Property 2: Lightness derivation holds chroma and hue
  // Validates: Requirements 5.3
  it('emits every non-500 step as oklch(from {hex} calc(l ±delta) c h) with strictly decreasing lightness offsets', () => {
    fc.assert(
      fc.property(arbRole, arbValidHex, (role, hex) => {
        const css = generateScale(role, hex);
        const lines = css.split('\n');
        const normalizedHex = normalizeHex(hex);

        const offsets: number[] = [];

        lines.forEach((line, index) => {
          const step = STEPS[index];
          if (step === 500) {
            offsets.push(0);
            return;
          }

          const match = line.match(
            /^--color-[a-z]+-\d+: oklch\(from (#[0-9a-fA-F]{6}) calc\(l ([+-]) ([0-9.]+)\) c h\);$/
          );
          expect(match).not.toBeNull();

          const [, matchedHex, sign, magnitudeStr] = match!;
          // Chroma (c) and hue (h) are held literal — the bare identifiers
          // 'c' and 'h' in the regex above already assert this — and the
          // hex used in the relative-color expression is the same input hex.
          expect(matchedHex).toBe(normalizedHex);

          const expectedDelta = LIGHTNESS_DELTA[step];
          const expectedSign = expectedDelta >= 0 ? '+' : '-';
          const expectedMagnitude = Math.abs(expectedDelta);

          expect(sign).toBe(expectedSign);
          expect(Number(magnitudeStr)).toBeCloseTo(expectedMagnitude, 10);

          const signedOffset = sign === '+' ? Number(magnitudeStr) : -Number(magnitudeStr);
          offsets.push(signedOffset);
        });

        // Lightness offsets strictly decrease from step 50 to step 950
        // (steps 50-400 lighter than 500, steps 600-950 darker than 500).
        for (let i = 1; i < offsets.length; i++) {
          expect(offsets[i]).toBeLessThan(offsets[i - 1]);
        }
      }),
      { numRuns: 100 }
    );
  });
});

/** Ordered roles as emitted by generateBrandTheme. */
const ROLES: readonly BrandColorRole[] = ['primary', 'secondary', 'tertiary'];

/** Arbitrary for a full valid BrandConfig; generateBrandTheme only reads config.colors,
 * so the other fields are arbitrary stubs that satisfy the BrandConfig shape. */
const arbBrandConfig: fc.Arbitrary<BrandConfig> = fc.record({
  logo: fc.record({ light: fc.string(), dark: fc.string() }),
  appName: fc.string({ minLength: 1, maxLength: 20 }),
  greetingTemplates: fc.array(fc.string({ minLength: 1, maxLength: 20 }), {
    minLength: 1,
    maxLength: 5,
  }),
  fallbackGreetings: fc.array(fc.string({ minLength: 1, maxLength: 20 }), {
    minLength: 1,
    maxLength: 5,
  }),
  colors: fc.record({
    primary: arbValidHex,
    secondary: arbValidHex,
    tertiary: arbValidHex,
  }),
});

/** Split a generateBrandTheme css string into its per-role 11-line blocks, in ROLES order. */
function splitRoleBlocks(css: string): Record<BrandColorRole, string> {
  const lines = css.split('\n');
  const blocks = {} as Record<BrandColorRole, string>;
  ROLES.forEach((role, index) => {
    blocks[role] = lines.slice(index * 11, index * 11 + 11).join('\n');
  });
  return blocks;
}

/**
 * Arbitrary pair of distinct 6-digit hex digit-strings (case-sensitive), used to
 * guarantee a role's changed hex normalizes to a different declaration block than
 * its original hex (normalizeHex only adds a leading '#'; it does not alter case
 * or digit content, so distinct digit-strings always normalize to distinct text).
 */
const arbDistinctHexPair = fc
  .tuple(arbValidHex, arbValidHex)
  .filter(([a, b]) => normalizeHex(a) !== normalizeHex(b));

describe('generateBrandTheme', () => {
  // Feature: branding-customization, Property 3: Generator determinism
  // Validates: Requirements 5.5
  it('produces character-for-character identical CSS when run twice on the same config', () => {
    fc.assert(
      fc.property(arbBrandConfig, (config) => {
        const first = generateBrandTheme(config);
        const second = generateBrandTheme(config);

        expect(second.css).toBe(first.css);
      }),
      { numRuns: 100 }
    );
  });

  // Feature: branding-customization, Property 3: Generator determinism
  // Validates: Requirements 5.5
  it('changing a single role hex changes only that role declarations, leaving the other two unchanged', () => {
    fc.assert(
      fc.property(arbBrandConfig, arbRole, arbDistinctHexPair, (config, changedRole, [originalHex, newHex]) => {
        const configA: BrandConfig = {
          ...config,
          colors: { ...config.colors, [changedRole]: originalHex },
        };
        const configB: BrandConfig = {
          ...config,
          colors: { ...config.colors, [changedRole]: newHex },
        };

        const blocksA = splitRoleBlocks(generateBrandTheme(configA).css);
        const blocksB = splitRoleBlocks(generateBrandTheme(configB).css);

        for (const role of ROLES) {
          if (role === changedRole) {
            expect(blocksB[role]).not.toBe(blocksA[role]);
          } else {
            expect(blocksB[role]).toBe(blocksA[role]);
          }
        }
      }),
      { numRuns: 100 }
    );
  });
});

/**
 * Arbitrary for an invalid hex color input: any string that does NOT match
 * HEX_COLOR_REGEX (/^#?[0-9a-fA-F]{6}$/) — covers wrong length (too short/
 * too long, with or without '#'), non-hex characters, empty strings, and
 * otherwise-valid hex with extra surrounding characters.
 */
const arbInvalidHex = fc
  .oneof(
    // Wrong length: 0-5 or 7-10 hex-alphabet chars, with optional '#'.
    fc
      .tuple(
        fc.array(fc.constantFrom(..."0123456789abcdefABCDEF".split('')), {
          minLength: 0,
          maxLength: 10,
        }),
        fc.boolean(),
      )
      .map(([digits, withHash]) => (withHash ? `#${digits.join('')}` : digits.join(''))),
    // Non-hex characters: 6 chars drawn from a non-hex alphabet, with optional '#'.
    fc
      .tuple(
        fc.array(fc.constantFrom(..."ghijklmnopqrstuvwxyz!@#$%^&*() ".split('')), {
          minLength: 6,
          maxLength: 6,
        }),
        fc.boolean(),
      )
      .map(([chars, withHash]) => (withHash ? `#${chars.join('')}` : chars.join(''))),
    // Otherwise-valid hex with extra surrounding characters.
    arbValidHex.map((hex) => `${hex}xx`),
    arbValidHex.map((hex) => `xx${hex}`),
    // Empty string.
    fc.constant(''),
  )
  .filter((value) => !HEX_COLOR_REGEX.test(value));

describe('generateBrandTheme invalid hex rejection', () => {
  // Feature: branding-customization, Property 4: Invalid hex rejection
  // Validates: Requirements 5.7, 8.5
  it('rejects an invalid hex for a role, falls back to the Default_Branding hex, and records a matching error', () => {
    fc.assert(
      fc.property(
        arbBrandConfig,
        arbRole,
        arbInvalidHex,
        (config, invalidRole, invalidHex) => {
          const testConfig: BrandConfig = {
            ...config,
            colors: { ...config.colors, [invalidRole]: invalidHex },
          };

          const { css, errors } = generateBrandTheme(testConfig);
          const blocks = splitRoleBlocks(css);

          // The invalid role's scale falls back to the Default_Branding hex
          // for that role: its step-500 declaration equals the normalized
          // default hex.
          const step500Line = blocks[invalidRole]
            .split('\n')
            [STEPS.indexOf(500)];
          expect(step500Line).toBe(
            `--color-${invalidRole}-500: ${normalizeHex(DEFAULT_COLORS[invalidRole])};`
          );

          // An error is recorded identifying both the offending value and the role.
          const matchingError = errors.find(
            (error) => error.field === `colors.${invalidRole}` && error.value === invalidHex
          );
          expect(matchingError).toBeDefined();
        }
      ),
      { numRuns: 100 }
    );
  });
});
