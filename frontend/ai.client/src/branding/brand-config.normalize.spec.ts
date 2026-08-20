// brand-config.normalize.spec.ts
//
// Property-based test for the BrandingService normalization/validation helpers
// (brand-config.normalize.ts). Covers Property 5 (config normalization supplies
// usable defaults). See design.md "Correctness Properties" for the authoritative
// property text and requirements.md 3.1, 4.1, 4.2, 7.5, 8.4 for the acceptance
// criteria being validated.
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { normalizeBrandConfig } from './brand-config.normalize';
import type { BrandConfig } from './brand.types';
import {
  DEFAULT_ALT_LABEL,
  DEFAULT_COLORS,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_LOGO,
} from './brand.defaults';

/** Mirrors the hex acceptance rule used by brand-config.normalize.ts (Requirement 5.1, 8.3). */
const HEX_COLOR_PATTERN = /^#?[0-9a-fA-F]{6}$/;

/** ---- Field-level validity/expectation models (mirroring the design.md validation rules table) ---- */

function isValidLogoPath(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isValidAppName(value: unknown): value is string {
  return typeof value === 'string' && value.length >= 1 && value.length <= 100 && /\S/.test(value);
}

function isValidHex(value: unknown): value is string {
  return typeof value === 'string' && HEX_COLOR_PATTERN.test(value);
}

/** Computes the expected normalized greeting array and whether defaulting/dropping occurred. */
function expectedGreetingList(
  raw: unknown,
  defaultList: readonly string[],
): { expected: string[]; wasAltered: boolean } {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > 50) {
    return { expected: [...defaultList], wasAltered: true };
  }
  const validEntries = raw.filter(
    (entry): entry is string => typeof entry === 'string' && entry.length >= 1 && entry.length <= 500,
  );
  if (validEntries.length === 0) {
    return { expected: [...defaultList], wasAltered: true };
  }
  return { expected: validEntries, wasAltered: validEntries.length !== raw.length };
}

/** ---- Arbitraries covering absent/empty/out-of-bounds/invalid variants for each field ---- */

const arbValidHex = fc
  .tuple(
    fc.array(fc.constantFrom(..."0123456789abcdefABCDEF".split('')), { minLength: 6, maxLength: 6 }),
    fc.boolean(),
  )
  .map(([digits, withHash]) => {
    const hex = digits.join('');
    return withHash ? `#${hex}` : hex;
  });

const arbInvalidHex = fc
  .oneof(
    fc
      .tuple(
        fc.array(fc.constantFrom(..."0123456789abcdefABCDEF".split('')), { minLength: 0, maxLength: 10 }),
        fc.boolean(),
      )
      .map(([digits, withHash]) => (withHash ? `#${digits.join('')}` : digits.join(''))),
    fc
      .tuple(
        fc.array(fc.constantFrom(..."ghijklmnopqrstuvwxyz!@#$%^&*() ".split('')), {
          minLength: 6,
          maxLength: 6,
        }),
        fc.boolean(),
      )
      .map(([chars, withHash]) => (withHash ? `#${chars.join('')}` : chars.join(''))),
    fc.constant(''),
    fc.constant(null),
    fc.integer(),
  )
  .filter((value) => !(typeof value === 'string' && HEX_COLOR_PATTERN.test(value)));

/** logo: undefined, null, {}, { light: '' }, { light: valid, dark: '' }, fully valid, wrong types, etc. */
const arbLogoPathValue = fc.oneof(
  fc.constant(undefined),
  fc.constant(''),
  fc.constant(null),
  fc.integer(),
  fc.string({ minLength: 1, maxLength: 30 }),
);
const arbLogo = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.record({ light: arbLogoPathValue, dark: arbLogoPathValue }, { requiredKeys: [] }),
);

/** appName: undefined, '', whitespace-only, too long (101+), valid, wrong types. */
const arbAppName = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.integer(),
  fc.constant(''),
  fc.constant('   \t  '),
  fc.string({ minLength: 101, maxLength: 150 }),
  fc.string({ minLength: 1, maxLength: 100 }).filter((s) => /\S/.test(s)),
);

/** greeting entries: valid (1-500 chars) vs invalid (empty, too long, wrong type). */
const arbGreetingEntryValid = fc.string({ minLength: 1, maxLength: 500 });
const arbGreetingEntryInvalid = fc.oneof(
  fc.constant(''),
  fc.string({ minLength: 501, maxLength: 550 }),
  fc.integer(),
  fc.constant(null),
);
const arbGreetingList = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.constant('not-an-array'),
  fc.constant([]), // empty array (out of bounds: below min 1)
  fc.array(fc.oneof(arbGreetingEntryValid, arbGreetingEntryInvalid), { minLength: 1, maxLength: 50 }), // mixed valid/invalid
  fc.array(arbGreetingEntryValid, { minLength: 51, maxLength: 55 }), // 51+ entries (out of bounds)
  fc.array(arbGreetingEntryValid, { minLength: 1, maxLength: 50 }), // fully valid
);

/** colors: undefined, {}, invalid hex per role, valid hex per role. */
const arbColorRoleValue = fc.oneof(fc.constant(undefined), fc.constant(null), arbInvalidHex, arbValidHex);
const arbColors = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.record(
    { primary: arbColorRoleValue, secondary: arbColorRoleValue, tertiary: arbColorRoleValue },
    { requiredKeys: [] },
  ),
);

/** The whole config: null/undefined, or a partial/possibly-invalid BrandConfig. */
const arbBrandConfigInput: fc.Arbitrary<Partial<BrandConfig> | null | undefined> = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.record(
    {
      logo: arbLogo,
      appName: arbAppName,
      greetingTemplates: arbGreetingList,
      fallbackGreetings: arbGreetingList,
      colors: arbColors,
    },
    { requiredKeys: [] },
  ) as fc.Arbitrary<Partial<BrandConfig>>,
);

describe('normalizeBrandConfig', () => {
  // Feature: branding-customization, Property 5: Config normalization supplies usable defaults
  // Validates: Requirements 3.1, 4.1, 4.2, 7.5, 8.4
  it('always returns usable values (valid provided value, else the Default_Branding value) and records an error per defaulted field', () => {
    fc.assert(
      fc.property(arbBrandConfigInput, (config) => {
        const result = normalizeBrandConfig(config);
        const raw = config ?? {};

        // --- logo.light / logo.dark ---
        const rawLight = (raw as Partial<BrandConfig>).logo?.light;
        const rawDark = (raw as Partial<BrandConfig>).logo?.dark;
        const lightValid = isValidLogoPath(rawLight);
        const darkValid = isValidLogoPath(rawDark);

        expect(typeof result.logo.light).toBe('string');
        expect(result.logo.light.length).toBeGreaterThan(0);
        expect(result.logo.light).toBe(lightValid ? rawLight : DEFAULT_LOGO.light);

        expect(typeof result.logo.dark).toBe('string');
        expect(result.logo.dark.length).toBeGreaterThan(0);
        expect(result.logo.dark).toBe(darkValid ? rawDark : DEFAULT_LOGO.dark);

        if (!lightValid) expect(result.errors.some((e) => e.field === 'logo.light')).toBe(true);
        if (!darkValid) expect(result.errors.some((e) => e.field === 'logo.dark')).toBe(true);

        // --- appName ---
        const rawAppName = (raw as Partial<BrandConfig>).appName;
        const appNameValid = isValidAppName(rawAppName);

        expect(typeof result.appName).toBe('string');
        expect(result.appName.length).toBeGreaterThanOrEqual(1);
        expect(result.appName.length).toBeLessThanOrEqual(100);
        expect(/\S/.test(result.appName)).toBe(true);
        expect(result.appName).toBe(appNameValid ? rawAppName : DEFAULT_ALT_LABEL);

        if (!appNameValid) expect(result.errors.some((e) => e.field === 'appName')).toBe(true);

        // --- greetingTemplates / fallbackGreetings ---
        const rawTemplates = (raw as Partial<BrandConfig>).greetingTemplates;
        const rawFallbacks = (raw as Partial<BrandConfig>).fallbackGreetings;

        const { expected: expectedTemplates, wasAltered: templatesAltered } = expectedGreetingList(
          rawTemplates,
          DEFAULT_GREETING_TEMPLATES,
        );
        const { expected: expectedFallbacks, wasAltered: fallbacksAltered } = expectedGreetingList(
          rawFallbacks,
          DEFAULT_FALLBACK_GREETINGS,
        );

        expect(Array.isArray(result.greetingTemplates)).toBe(true);
        expect(result.greetingTemplates.length).toBeGreaterThan(0);
        result.greetingTemplates.forEach((entry) => {
          expect(typeof entry).toBe('string');
          expect(entry.length).toBeGreaterThanOrEqual(1);
          expect(entry.length).toBeLessThanOrEqual(500);
        });
        expect(result.greetingTemplates).toEqual(expectedTemplates);
        if (templatesAltered) {
          expect(result.errors.some((e) => e.field === 'greetingTemplates')).toBe(true);
        }

        expect(Array.isArray(result.fallbackGreetings)).toBe(true);
        expect(result.fallbackGreetings.length).toBeGreaterThan(0);
        result.fallbackGreetings.forEach((entry) => {
          expect(typeof entry).toBe('string');
          expect(entry.length).toBeGreaterThanOrEqual(1);
          expect(entry.length).toBeLessThanOrEqual(500);
        });
        expect(result.fallbackGreetings).toEqual(expectedFallbacks);
        if (fallbacksAltered) {
          expect(result.errors.some((e) => e.field === 'fallbackGreetings')).toBe(true);
        }

        // --- colors.{primary,secondary,tertiary} ---
        (['primary', 'secondary', 'tertiary'] as const).forEach((role) => {
          const rawColor = (raw as Partial<BrandConfig>).colors?.[role];
          const colorValid = isValidHex(rawColor);

          expect(typeof result.colors[role]).toBe('string');
          expect(HEX_COLOR_PATTERN.test(result.colors[role])).toBe(true);
          expect(result.colors[role]).toBe(colorValid ? rawColor : DEFAULT_COLORS[role]);

          if (!colorValid) {
            expect(result.errors.some((e) => e.field === `colors.${role}`)).toBe(true);
          }
        });
      }),
      { numRuns: 100 },
    );
  });
});
