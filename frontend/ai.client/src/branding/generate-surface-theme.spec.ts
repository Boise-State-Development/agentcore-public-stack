// generate-surface-theme.spec.ts
//
// Property-based tests for the Surface_Ramp_Generator
// (scripts/branding/generate-surface-theme.ts). Mirrors the structure of
// generate-brand-theme.spec.ts: arbitraries for in-band surface hexes,
// then property tests for the ramp's defining invariants.
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import {
  applyContrastClamp,
  generateSurfaceRamp,
  generateSurfaceTheme,
  STEPS,
  TAILWIND_GRAY_RAMP,
} from '../../scripts/branding/generate-surface-theme';
import type { BrandConfig, BrandSurfaces } from './brand.types';
import { DEFAULT_SURFACES } from './brand.defaults';
import { contrastRatio, hexToOklch, normalizeHex, oklchToSrgb } from '../../scripts/branding/color-math';
import { generateBrandTheme } from '../../scripts/branding/generate-brand-theme';

/** Build a valid 6-digit hex from an OKLCH triple, for constructing in-band anchors deterministically. */
function oklchToHex(l: number, c: number, h: number): string {
  const [r, g, b] = oklchToSrgb(l, c, h).map((v) => Math.round(v * 255));
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

/** In-band light anchor: OKLCH lightness >= 0.90, chroma <= 0.04 (mirrors SURFACE_BANDS.light). */
const arbLightHex = fc
  .tuple(fc.double({ min: 0.9, max: 1, noNaN: true }), fc.double({ min: 0, max: 0.04, noNaN: true }), fc.double({ min: 0, max: 360, noNaN: true }))
  .map(([l, c, h]) => oklchToHex(l, c, h));

/** In-band dark anchor: OKLCH lightness <= 0.32, chroma <= 0.05 (mirrors SURFACE_BANDS.dark). */
const arbDarkHex = fc
  .tuple(fc.double({ min: 0, max: 0.32, noNaN: true }), fc.double({ min: 0, max: 0.05, noNaN: true }), fc.double({ min: 0, max: 360, noNaN: true }))
  .map(([l, c, h]) => oklchToHex(l, c, h));

describe('generateSurfaceRamp', () => {
  // Zero-diff property: at DEFAULT_SURFACES, the ramp is TAILWIND_GRAY_RAMP verbatim.
  it('emits TAILWIND_GRAY_RAMP character-for-character at DEFAULT_SURFACES', () => {
    const ramp = generateSurfaceRamp({ light: DEFAULT_SURFACES.light, dark: DEFAULT_SURFACES.dark });

    for (const step of STEPS) {
      const expectedLine = `--color-gray-${step}: oklch(${TAILWIND_GRAY_RAMP[step].text});`;
      expect(ramp[step].line).toBe(expectedLine);
    }
  });

  // Endpoint exactness: step 50 renders the light anchor, step 900 the dark anchor.
  it('renders the light anchor exactly at step 50 and the dark anchor exactly at step 900, for arbitrary in-band anchors', () => {
    fc.assert(
      fc.property(arbLightHex, arbDarkHex, (lightHex, darkHex) => {
        const ramp = generateSurfaceRamp({ light: lightHex, dark: darkHex });
        const lightOklch = hexToOklch(lightHex);
        const darkOklch = hexToOklch(darkHex);

        expect(ramp[50].l).toBeCloseTo(lightOklch.l, 9);
        expect(ramp[50].c).toBeCloseTo(lightOklch.c, 9);
        // Hue is only meaningful when chroma is non-trivial; skip hue check
        // when either chroma is ~0 (achromatic, hue is arbitrary/unstable).
        if (lightOklch.c > 1e-6) {
          expect(((ramp[50].h - lightOklch.h + 540) % 360) - 180).toBeCloseTo(0, 6);
        }

        expect(ramp[900].l).toBeCloseTo(darkOklch.l, 9);
        expect(ramp[900].c).toBeCloseTo(darkOklch.c, 9);
        if (darkOklch.c > 1e-6) {
          expect(((ramp[900].h - darkOklch.h + 540) % 360) - 180).toBeCloseTo(0, 6);
        }
      }),
      { numRuns: 100 },
    );
  });

  // Monotonicity: lightness strictly decreases across all 11 steps.
  it('produces strictly decreasing lightness across all 11 steps, for arbitrary in-band anchors', () => {
    fc.assert(
      fc.property(arbLightHex, arbDarkHex, (lightHex, darkHex) => {
        const ramp = generateSurfaceRamp({ light: lightHex, dark: darkHex });
        const lightnesses = STEPS.map((step) => ramp[step].l);

        for (let i = 1; i < lightnesses.length; i++) {
          expect(lightnesses[i]).toBeLessThan(lightnesses[i - 1]);
        }
      }),
      { numRuns: 100 },
    );
  });

  // Determinism: identical inputs -> identical output, across two runs.
  it('produces identical output across two runs on the same input', () => {
    fc.assert(
      fc.property(arbLightHex, arbDarkHex, (lightHex, darkHex) => {
        const first = generateSurfaceRamp({ light: lightHex, dark: darkHex });
        const second = generateSurfaceRamp({ light: lightHex, dark: darkHex });

        for (const step of STEPS) {
          expect(second[step]).toEqual(first[step]);
        }
      }),
      { numRuns: 100 },
    );
  });

  // Hue wrap: anchors straddling the 0/360 boundary produce a short-arc hue
  // *delta* against their anchor step, so the endpoint hues land exactly
  // and no step's hue offset swings the long way around. (Intermediate
  // steps' own hue is base_hue + blended_offset, not a direct interpolation
  // between the two anchor hues, since each Tailwind step has its own base
  // hue — so this checks the endpoints, which is where the wrap matters.)
  it('lands the exact anchor hue at steps 50 and 900 when anchors straddle 0°/360°', () => {
    // Light anchor at hue ~5°, dark anchor at hue ~355° — the short arc
    // between them is 10° wide; a naive unwrapped interpolation could
    // instead compute a ~350° delta against the base ramp's own hue and
    // land far from the configured anchor.
    const lightHex = oklchToHex(0.95, 0.03, 5);
    const darkHex = oklchToHex(0.2, 0.03, 355);

    const ramp = generateSurfaceRamp({ light: lightHex, dark: darkHex });
    const lightOklch = hexToOklch(lightHex);
    const darkOklch = hexToOklch(darkHex);

    expect(((ramp[50].h - lightOklch.h + 540) % 360) - 180).toBeCloseTo(0, 6);
    expect(((ramp[900].h - darkOklch.h + 540) % 360) - 180).toBeCloseTo(0, 6);
  });
});

describe('generateSurfaceTheme', () => {
  const baseConfig: BrandConfig = {
    logo: { light: 'a.png', dark: 'b.png' },
    appName: 'Test App',
    greetingTemplates: ['hi {name}'],
    fallbackGreetings: ['hi'],
    colors: { primary: '#0033a0', secondary: '#d64309', tertiary: '#0072ce' },
    pageTitle: 'Test',
    surfaces: { ...DEFAULT_SURFACES },
  };

  it('emits --color-white from surfaces.raised at Default_Surfaces', () => {
    const { css, errors } = generateSurfaceTheme(baseConfig);
    expect(css).toContain('--color-white: #ffffff;');
    // No hex-validation errors are recorded. The one entry present at
    // defaults is the documented step-500 opposing-constraint warning
    // (see applyContrastClamp's "inert at DEFAULT_SURFACES" test above),
    // not a fallback/rejection error.
    expect(errors.every((e) => e.field === 'surfaces.gray-500')).toBe(true);
  });

  it('falls back to Default_Surfaces and records a BrandConfigError on an invalid hex', () => {
    const config: BrandConfig = {
      ...baseConfig,
      surfaces: { light: 'not-a-hex', dark: DEFAULT_SURFACES.dark, raised: DEFAULT_SURFACES.raised },
    };
    const { css, errors } = generateSurfaceTheme(config);

    expect(errors.some((e) => e.field === 'surfaces.light')).toBe(true);
    // Falls back to the default light anchor, so the ramp is unaffected.
    expect(css).toContain(`--color-gray-50: oklch(${TAILWIND_GRAY_RAMP[50].text});`);
  });

  it('produces identical css when run twice on the same config', () => {
    const first = generateSurfaceTheme(baseConfig);
    const second = generateSurfaceTheme(baseConfig);
    expect(second.css).toBe(first.css);
  });
});

/** Thin wrapper so the cross-generator test below reads naturally. */
function generateAccessibleAliasesFrom(config: BrandConfig): { css: string } {
  return generateBrandTheme(config);
}

/**
 * Parse the emitted `--color-{role}-{name}` declaration out of
 * generateBrandTheme's output and resolve it to an sRGB triple, handling
 * both the literal-hex form (delta 0) and the
 * `oklch(from #hex calc(l ± delta) c h)` relative-color form.
 */
function resolveAccessibleAliasSrgb(css: string, role: string, name: string): [number, number, number] {
  const lineRegex = new RegExp(`--color-${role}-${name}: (.+);`);
  const match = css.match(lineRegex);
  if (!match) throw new Error(`--color-${role}-${name} not found in generated css`);
  const value = match[1];

  const literalHexMatch = value.match(/^#[0-9a-fA-F]{6}$/);
  if (literalHexMatch) {
    const { l, c, h } = hexToOklch(normalizeHex(value));
    return oklchToSrgb(l, c, h);
  }

  const relativeMatch = value.match(/^oklch\(from (#[0-9a-fA-F]{6}) calc\(l ([+-]) ([0-9.]+)\) c h\)$/);
  if (!relativeMatch) throw new Error(`Unrecognized declaration form: ${value}`);
  const [, hex, sign, magnitudeStr] = relativeMatch;
  const { l, c, h } = hexToOklch(hex);
  const delta = sign === '+' ? Number(magnitudeStr) : -Number(magnitudeStr);
  return oklchToSrgb(l + delta, c, h);
}

/**
 * In-band raised anchor: OKLCH lightness >= 0.95, chroma <= 0.03 (mirrors
 * SURFACE_BANDS.raised), AND lightness >= DEFAULT_SURFACES.light's own
 * lightness (~0.9846), so the arbitrary also satisfies the `raised >=
 * light` cross-field check when paired with the default light anchor
 * (as every use below does). Constructing a hex via oklchToHex quantizes
 * to 8-bit sRGB, and the subsequent hexToOklch round-trip a real band/
 * cross-field check (added by cluster B) performs can land a hair under
 * a boundary value that was in-band before quantization. Rather than
 * guess a margin large enough to absorb the quantization error, filter
 * on the actual round-tripped value so every emitted hex is guaranteed
 * in-band (and cross-field-valid) by the same checks the generator
 * applies.
 */
const DEFAULT_LIGHT_L = hexToOklch(DEFAULT_SURFACES.light).l;
const arbRaisedHex = fc
  .tuple(fc.double({ min: 0.95, max: 1, noNaN: true }), fc.double({ min: 0, max: 0.03, noNaN: true }), fc.double({ min: 0, max: 360, noNaN: true }))
  .map(([l, c, h]) => oklchToHex(l, c, h))
  .filter((hex) => {
    const { l, c } = hexToOklch(hex);
    return l >= 0.95 && c <= 0.03 && l >= DEFAULT_LIGHT_L;
  });

describe('generateSurfaceTheme --color-white (Task 6)', () => {
  const baseConfig: BrandConfig = {
    logo: { light: 'a.png', dark: 'b.png' },
    appName: 'Test App',
    greetingTemplates: ['hi {name}'],
    fallbackGreetings: ['hi'],
    colors: { primary: '#0033a0', secondary: '#d64309', tertiary: '#0072ce' },
    pageTitle: 'Test',
    surfaces: { ...DEFAULT_SURFACES },
  };

  it('emits --color-white: #ffffff at Default_Surfaces', () => {
    const { css } = generateSurfaceTheme(baseConfig);
    expect(css).toContain('--color-white: #ffffff;');
  });

  it('emits a configured surfaces.raised value verbatim', () => {
    fc.assert(
      fc.property(arbRaisedHex, (raisedHex) => {
        const config: BrandConfig = {
          ...baseConfig,
          surfaces: { light: DEFAULT_SURFACES.light, dark: DEFAULT_SURFACES.dark, raised: raisedHex },
        };
        const { css } = generateSurfaceTheme(config);
        const normalized = raisedHex.startsWith('#') ? raisedHex : `#${raisedHex}`;
        expect(css).toContain(`--color-white: ${normalized};`);
      }),
      { numRuns: 50 },
    );
  });

  // White text on a `bg-{role}-accessible` fill still clears 4.5:1 for any
  // in-band `surfaces.raised` value. generate-brand-theme.ts's accessible
  // alias resolves its light reference as the *darker* of
  // surfaces.light/.raised (Task 5), so the alias's own findAccessibleLightnessDelta
  // guarantee against that reference is, by construction, at least as
  // strict as a guarantee against pure white — since the reference can
  // only be darker than or equal to white, never lighter.
  it('the primary-accessible alias still clears 4.5:1 against white, for any in-band raised value', () => {
    fc.assert(
      fc.property(arbRaisedHex, (raisedHex) => {
        const config: BrandConfig = {
          ...baseConfig,
          colors: { primary: '#ffcc00', secondary: '#d64309', tertiary: '#0072ce' }, // a light, contrast-hostile brand hue
          surfaces: { light: DEFAULT_SURFACES.light, dark: DEFAULT_SURFACES.dark, raised: raisedHex },
        };
        const { css } = generateAccessibleAliasesFrom(config);
        const white: [number, number, number] = [1, 1, 1];

        // Extract the emitted --color-primary-accessible declaration and
        // resolve it to an sRGB triple, then assert it clears AA against
        // white directly (not just against the alias's own reference).
        const srgb = resolveAccessibleAliasSrgb(css, 'primary', 'accessible');
        expect(contrastRatio(srgb, white)).toBeGreaterThanOrEqual(4.5 - 1e-6);
      }),
      { numRuns: 50 },
    );
  });
});

describe('applyContrastClamp', () => {
  const LIGHT_TEXT_STEPS = [500, 600, 700, 900] as const;
  const DARK_TEXT_STEPS = [200, 300, 400] as const;

  it('is inert at DEFAULT_SURFACES, preserving the zero-diff ramp (aside from the documented step-500 warning)', () => {
    const ramp = generateSurfaceRamp({ light: DEFAULT_SURFACES.light, dark: DEFAULT_SURFACES.dark });
    const errors: ReturnType<typeof generateSurfaceTheme>['errors'] = [];
    const clamped = applyContrastClamp(ramp, DEFAULT_SURFACES, errors);

    for (const step of STEPS) {
      expect(clamped[step].line).toBe(ramp[step].line);
    }
    // The only error recorded is the documented step-500 opposing-constraint warning.
    expect(errors.every((e) => e.field === 'surfaces.gray-500')).toBe(true);
  });

  // Property test: every enforced text/surface pair clears 4.5:1 in the emitted ramp, for arbitrary in-band anchors.
  it('clears 4.5:1 for every enforced light/dark text step, for arbitrary in-band anchor pairs', () => {
    fc.assert(
      fc.property(arbLightHex, arbDarkHex, (lightHex, darkHex) => {
        const surfaces: BrandSurfaces = { light: lightHex, dark: darkHex, raised: DEFAULT_SURFACES.raised };
        const ramp = generateSurfaceRamp({ light: lightHex, dark: darkHex });
        const errors: ReturnType<typeof generateSurfaceTheme>['errors'] = [];
        const clamped = applyContrastClamp(ramp, surfaces, errors);

        const lightOklch = hexToOklch(lightHex);
        const raisedOklch = hexToOklch(surfaces.raised);
        const lightBgOklch = lightOklch.l <= raisedOklch.l ? lightOklch : raisedOklch;
        const lightBg = oklchToSrgb(lightBgOklch.l, lightBgOklch.c, lightBgOklch.h);
        const darkBg = oklchToSrgb(...(Object.values(hexToOklch(darkHex)) as [number, number, number]));

        for (const step of LIGHT_TEXT_STEPS) {
          const entry = clamped[step];
          const srgb = oklchToSrgb(entry.l, entry.c, entry.h);
          expect(contrastRatio(srgb, lightBg)).toBeGreaterThanOrEqual(4.5 - 1e-6);
        }

        for (const step of DARK_TEXT_STEPS) {
          const entry = clamped[step];
          const srgb = oklchToSrgb(entry.l, entry.c, entry.h);
          expect(contrastRatio(srgb, darkBg)).toBeGreaterThanOrEqual(4.5 - 1e-6);
        }
      }),
      { numRuns: 50 },
    );
  });

  it('clamps a deliberately aggressive-but-in-band anchor pair and records the expected warning', () => {
    // A near-midtone "light" anchor (still >= 0.90 lightness per the band,
    // but with enough chroma to be a hostile case) paired with a near-
    // midtone "dark" anchor, to force several text steps to fail contrast
    // before clamping.
    const lightHex = oklchToHex(0.9, 0.04, 90);
    const darkHex = oklchToHex(0.3, 0.05, 90);

    const ramp = generateSurfaceRamp({ light: lightHex, dark: darkHex });
    const surfaces: BrandSurfaces = { light: lightHex, dark: darkHex, raised: DEFAULT_SURFACES.raised };
    const errors: ReturnType<typeof generateSurfaceTheme>['errors'] = [];
    const clamped = applyContrastClamp(ramp, surfaces, errors);

    const lightBg = oklchToSrgb(...(Object.values(hexToOklch(lightHex)) as [number, number, number]));
    const darkBg = oklchToSrgb(...(Object.values(hexToOklch(darkHex)) as [number, number, number]));

    for (const step of LIGHT_TEXT_STEPS) {
      const entry = clamped[step];
      const srgb = oklchToSrgb(entry.l, entry.c, entry.h);
      expect(contrastRatio(srgb, lightBg)).toBeGreaterThanOrEqual(4.5 - 1e-6);
    }
    for (const step of DARK_TEXT_STEPS) {
      const entry = clamped[step];
      const srgb = oklchToSrgb(entry.l, entry.c, entry.h);
      expect(contrastRatio(srgb, darkBg)).toBeGreaterThanOrEqual(4.5 - 1e-6);
    }

    // At least one step was clamped, and each clamp recorded an explanatory error.
    expect(errors.length).toBeGreaterThan(0);
  });
});
