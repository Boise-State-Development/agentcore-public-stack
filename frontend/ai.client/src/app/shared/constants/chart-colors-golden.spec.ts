// chart-colors-golden.spec.ts
//
// Golden regression test for CHART_CHROME_COLORS (Task 7). Asserts that,
// at Default_Surfaces, the generated chart chrome colors match the
// historical hand-written hex values exactly — so migrating
// chart-colors.constants.ts to read from the build-time-generated
// surface-colors.ts module changes no pixel in the admin cost dashboard
// or inline chart tooltips under a clean checkout.
import { describe, it, expect } from 'vitest';
import { CHART_CHROME_COLORS, getChromeColorsForMode } from './chart-colors.constants';

describe('CHART_CHROME_COLORS golden regression (Default_Surfaces)', () => {
  it('matches the historical hand-written light-mode chrome colors', () => {
    expect(CHART_CHROME_COLORS.light).toEqual({
      background: '#ffffff',
      titleText: '#111827', // gray-900
      bodyText: '#4b5563',
      border: '#e5e7eb', // gray-200
      axisText: '#6b7280', // gray-500
      gridLine: 'rgba(0, 0, 0, 0.1)',
    });
  });

  it('matches the historical hand-written dark-mode chrome colors', () => {
    expect(CHART_CHROME_COLORS.dark).toEqual({
      background: '#1f2937', // gray-800
      titleText: '#ffffff',
      bodyText: '#d1d5db', // gray-300
      border: '#374151', // gray-700
      axisText: '#9ca3af', // gray-400
      gridLine: 'rgba(255, 255, 255, 0.1)',
    });
  });

  it('getChromeColorsForMode still returns the matching mode', () => {
    expect(getChromeColorsForMode(false)).toEqual(CHART_CHROME_COLORS.light);
    expect(getChromeColorsForMode(true)).toEqual(CHART_CHROME_COLORS.dark);
  });
});
