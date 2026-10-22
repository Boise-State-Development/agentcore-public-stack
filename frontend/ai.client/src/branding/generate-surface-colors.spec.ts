// generate-surface-colors.spec.ts
//
// Tests for the Surface_Colors_Generator (Task 7). Covers the zero-diff
// golden property (matches the historical hand-written CHART_CHROME_COLORS
// at Default_Surfaces) and basic shape/determinism.
import { describe, it, expect } from 'vitest';
import { generateSurfaceColors, resolveSurfaceColors } from '../../scripts/branding/generate-surface-colors';
import { DEFAULT_SURFACES } from './brand.defaults';
import { BRAND_CONFIG } from './brand.config';

describe('resolveSurfaceColors', () => {
  it('matches the historical hand-written CHART_CHROME_COLORS at Default_Surfaces', () => {
    const colors = resolveSurfaceColors(DEFAULT_SURFACES);

    expect(colors.light).toEqual({
      background: '#ffffff',
      titleText: '#111827',
      bodyText: '#4b5563',
      border: '#e5e7eb',
      axisText: '#6b7280',
      gridLine: 'rgba(0, 0, 0, 0.1)',
    });
    expect(colors.dark).toEqual({
      background: '#1f2937',
      titleText: '#ffffff',
      bodyText: '#d1d5db',
      border: '#374151',
      axisText: '#9ca3af',
      gridLine: 'rgba(255, 255, 255, 0.1)',
    });
  });

  it('reflects a configured raised value in light.background and dark.titleText', () => {
    const colors = resolveSurfaceColors({ ...DEFAULT_SURFACES, raised: '#fef8f0' });
    expect(colors.light.background).toBe('#fef8f0');
    expect(colors.dark.titleText).toBe('#fef8f0');
  });

  it('produces identical output across two runs on the same input', () => {
    const surfaces = { light: '#f5f5f0', dark: '#0f1115', raised: '#ffffff' };
    const first = resolveSurfaceColors(surfaces);
    const second = resolveSurfaceColors(surfaces);
    expect(second).toEqual(first);
  });
});

describe('generateSurfaceColors', () => {
  it('resolves against the live BRAND_CONFIG, recording only surfaces.* band rejections', () => {
    const { colors, errors } = generateSurfaceColors(BRAND_CONFIG);
    // BRAND_CONFIG.surfaces.raised renders verbatim if in-band (as
    // configured today), or falls back to DEFAULT_SURFACES.raised
    // (#ffffff) if rejected — either way light.background tracks
    // whatever `raised` resolves to, and any recorded error names a
    // surfaces.* field, never an unrelated one.
    expect(errors.every((e) => e.field.startsWith('surfaces.'))).toBe(true);
    expect([BRAND_CONFIG.surfaces.raised, '#ffffff']).toContain(colors.light.background);
  });

  it('resolves an explicit DEFAULT_SURFACES config with zero errors', () => {
    const { colors, errors } = generateSurfaceColors({ ...BRAND_CONFIG, surfaces: { ...DEFAULT_SURFACES } });
    expect(colors.light.background).toBe('#ffffff');
    expect(errors).toEqual([]);
  });
});
