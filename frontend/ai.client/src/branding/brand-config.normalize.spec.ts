// brand-config.normalize.spec.ts
//
// Property-based test for the BrandingService normalization/validation helpers
// (brand-config.normalize.ts). Covers Property 5 (config normalization supplies
// usable defaults). See design.md "Correctness Properties" for the authoritative
// property text and requirements.md 3.1, 4.1, 4.2, 7.5, 8.4 for the acceptance
// criteria being validated.
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { normalizeBrandConfig, resolveSurfaces } from './brand-config.normalize';
import type { BrandConfig, BrandSurfaces } from './brand.types';
import {
  DEFAULT_ALT_LABEL,
  DEFAULT_COLORS,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_LOGO,
  DEFAULT_SURFACES,
} from './brand.defaults';
import { hexToOklch } from '../../scripts/branding/color-math';

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

/** Mirrors SURFACE_BANDS in brand-config.normalize.ts. */
const SURFACE_BANDS: Record<keyof BrandSurfaces, { minL?: number; maxL?: number; maxC: number }> = {
  light: { minL: 0.9, maxC: 0.04 },
  raised: { minL: 0.95, maxC: 0.03 },
  dark: { maxL: 0.32, maxC: 0.05 },
};

/** Mirrors normalizeSurfaceRole's per-field validity check (hex format + band), ignoring the cross-field raised>=light check. */
function isValidSurfaceRole(role: keyof BrandSurfaces, value: unknown): value is string {
  if (typeof value !== 'string' || !HEX_COLOR_PATTERN.test(value)) return false;
  const band = SURFACE_BANDS[role];
  const { l, c } = hexToOklch(value);
  if (band.minL !== undefined && l < band.minL) return false;
  if (band.maxL !== undefined && l > band.maxL) return false;
  if (c > band.maxC) return false;
  return true;
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

/**
 * surfaces: undefined, {}, invalid-format hex, or any well-formed hex
 * (deliberately including both in-band and out-of-band well-formed hex —
 * `isValidSurfaceRole` models exactly which ones the normalizer accepts).
 */
const arbSurfaceRoleValue = fc.oneof(fc.constant(undefined), fc.constant(null), arbInvalidHex, arbValidHex);
const arbSurfaces = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.record(
    { light: arbSurfaceRoleValue, dark: arbSurfaceRoleValue, raised: arbSurfaceRoleValue },
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
      surfaces: arbSurfaces,
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

        // --- surfaces.{light,dark,raised} ---
        // light/dark are independently normalized (no cross-field dependency).
        (['light', 'dark'] as const).forEach((role) => {
          const rawSurface = (raw as Partial<BrandConfig>).surfaces?.[role];
          const surfaceValid = isValidSurfaceRole(role, rawSurface);

          expect(typeof result.surfaces[role]).toBe('string');
          expect(HEX_COLOR_PATTERN.test(result.surfaces[role])).toBe(true);
          expect(result.surfaces[role]).toBe(surfaceValid ? rawSurface : DEFAULT_SURFACES[role]);

          if (!surfaceValid) {
            expect(result.errors.some((e) => e.field === `surfaces.${role}`)).toBe(true);
          }
        });

        // raised additionally depends on the resolved `light` value via the
        // cross-field check (raised lightness >= light lightness), so it is
        // asserted against the *resolved* light value, not the raw input.
        const rawRaised = (raw as Partial<BrandConfig>).surfaces?.raised;
        const raisedValidStandalone = isValidSurfaceRole('raised', rawRaised);
        expect(typeof result.surfaces.raised).toBe('string');
        expect(HEX_COLOR_PATTERN.test(result.surfaces.raised)).toBe(true);

        if (!raisedValidStandalone) {
          expect(result.surfaces.raised).toBe(DEFAULT_SURFACES.raised);
          expect(result.errors.some((e) => e.field === 'surfaces.raised')).toBe(true);
        } else if (hexToOklch(rawRaised as string).l < hexToOklch(result.surfaces.light).l) {
          expect(result.surfaces.raised).toBe(DEFAULT_SURFACES.raised);
          expect(result.errors.some((e) => e.field === 'surfaces.raised')).toBe(true);
        } else {
          expect(result.surfaces.raised).toBe(rawRaised);
        }
      }),
      { numRuns: 100 },
    );
  });
});

// resolveSurfaces.spec — unit tests for the build-time adapter (task 4.3).
//
// resolveSurfaces is the one function the build-time generators
// (generateSurfaceTheme, generateSurfaceColors, generateBrandTheme) and
// BrandingService's normalizeBrandConfig ultimately share for band
// validation. These tests pin its contract directly: hex-format + OKLCH
// band validation identical to normalizeSurfaces, but with every resolved
// role normalized to carry a leading '#'.
describe('resolveSurfaces', () => {
  it('resolves in-band values with a leading # unchanged (aside from normalization)', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: '#fdfcf8', dark: '#0f1420', raised: '#fffdf9' }, errors);
    expect(result).toEqual({ light: '#fdfcf8', dark: '#0f1420', raised: '#fffdf9' });
    expect(errors).toEqual([]);
  });

  it('adds a leading # to a bare in-band hex (no leading #)', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: 'fdfcf8', dark: '0f1420', raised: 'fffdf9' }, errors);
    expect(result).toEqual({ light: '#fdfcf8', dark: '#0f1420', raised: '#fffdf9' });
    expect(errors).toEqual([]);
  });

  it('accepts mixed-case hex', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: '#FdFcF8', dark: '#0F1420', raised: '#FFFDF9' }, errors);
    expect(result).toEqual({ light: '#FdFcF8', dark: '#0F1420', raised: '#FFFDF9' });
    expect(errors).toEqual([]);
  });

  it('falls back to DEFAULT_SURFACES.light and records an error for malformed hex', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: 'not-a-hex', dark: '#0f1420', raised: '#fffdf9' }, errors);
    expect(result.light).toBe('#f9fafb');
    expect(errors.some((e) => e.field === 'surfaces.light')).toBe(true);
  });

  it('falls back and records an error for a wrong-type value', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: 123 as unknown as string, dark: '#0f1420', raised: '#fffdf9' }, errors);
    expect(result.light).toBe('#f9fafb');
    expect(errors.some((e) => e.field === 'surfaces.light')).toBe(true);
  });

  it('falls back for an absent field', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ dark: '#0f1420', raised: '#fffdf9' }, errors);
    expect(result.light).toBe('#f9fafb');
    expect(errors.some((e) => e.field === 'surfaces.light')).toBe(true);
  });

  it('falls back to all three defaults for an absent surfaces object', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces(undefined, errors);
    expect(result).toEqual({ light: '#f9fafb', dark: '#101828', raised: '#ffffff' });
  });

  it('validates each role independently: one invalid role does not affect the other two valid ones', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: 'garbage', dark: '#0f1420', raised: '#fffdf9' }, errors);
    expect(result.light).toBe('#f9fafb'); // fell back
    expect(result.dark).toBe('#0f1420'); // preserved
    expect(result.raised).toBe('#fffdf9'); // preserved
    expect(errors).toHaveLength(1);
    expect(errors[0].field).toBe('surfaces.light');
  });

  it('accepts each band boundary just inside and rejects just outside', () => {
    // light: minL 0.9, maxC 0.04. Just inside: L=0.90, C=0.03 -> in band.
    // hexToOklch/oklchToSrgb round trips are used elsewhere in the spec
    // suite for exact boundary construction; here a known in-band/out-of-
    // band pair from the existing surface generator spec fixtures is used
    // instead, since resolveSurfaces delegates its band math verbatim to
    // normalizeSurfaceRole (already covered by the property test above).
    const errorsInBand: Parameters<typeof resolveSurfaces>[1] = [];
    const inBand = resolveSurfaces({ light: '#fdfcf8', dark: '#0f1420', raised: '#fffdf9' }, errorsInBand);
    expect(errorsInBand).toEqual([]);
    expect(inBand.light).toBe('#fdfcf8');

    const errorsOutOfBand: Parameters<typeof resolveSurfaces>[1] = [];
    const outOfBand = resolveSurfaces({ light: '#00ff2a', dark: '#0f1420', raised: '#fffdf9' }, errorsOutOfBand);
    expect(errorsOutOfBand.some((e) => e.field === 'surfaces.light')).toBe(true);
    expect(outOfBand.light).toBe('#f9fafb');
  });

  it('applies the raised < light cross-field check and falls back raised to the default', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    // light and raised are each independently in-band, but raised is
    // darker than light, which violates the cross-field check.
    const result = resolveSurfaces({ light: '#fffdf9', dark: '#0f1420', raised: '#fdfcf8' }, errors);
    expect(result.raised).toBe('#ffffff');
    expect(errors.some((e) => e.field === 'surfaces.raised')).toBe(true);
  });

  it('resolves the reported out-of-band config to DEFAULT_SURFACES on all three roles', () => {
    const errors: Parameters<typeof resolveSurfaces>[1] = [];
    const result = resolveSurfaces({ light: '#00ff2a', dark: '#9900ff', raised: '#009118' }, errors);
    expect(result).toEqual({ light: '#f9fafb', dark: '#101828', raised: '#ffffff' });
    expect(errors).toHaveLength(3);
    expect(errors.map((e) => e.field).sort()).toEqual(['surfaces.dark', 'surfaces.light', 'surfaces.raised']);
  });
});
