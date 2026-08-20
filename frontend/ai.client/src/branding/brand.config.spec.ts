// brand.config.spec.ts
//
// Shape/example tests for Brand_Config (src/branding/brand.config.ts).
// Asserts BRAND_CONFIG exposes every named field with the expected
// type/shape. Deliberately does NOT assert the values equal
// Default_Branding: brand.config.ts is the Forker's edit surface, so
// pinning it to the defaults would fail for every rebranded fork. The
// defaults themselves are pinned separately in
// brand.defaults.golden.spec.ts. See design.md "Data Models" and
// requirements.md 1.1, 1.2, 2.1, 8.1.
import { describe, it, expect } from 'vitest';
import { BRAND_CONFIG } from './brand.config';

const HEX_COLOR_REGEX = /^#?[0-9a-fA-F]{6}$/;

describe('Brand_Config shape', () => {
  it('exposes logo.light and logo.dark as non-empty strings', () => {
    expect(typeof BRAND_CONFIG.logo.light).toBe('string');
    expect(BRAND_CONFIG.logo.light.length).toBeGreaterThan(0);

    expect(typeof BRAND_CONFIG.logo.dark).toBe('string');
    expect(BRAND_CONFIG.logo.dark.length).toBeGreaterThan(0);
  });

  it('exposes appName as a non-empty string', () => {
    expect(typeof BRAND_CONFIG.appName).toBe('string');
    expect(BRAND_CONFIG.appName.length).toBeGreaterThan(0);
  });

  it('exposes greetingTemplates as a non-empty array of strings', () => {
    expect(Array.isArray(BRAND_CONFIG.greetingTemplates)).toBe(true);
    expect(BRAND_CONFIG.greetingTemplates.length).toBeGreaterThan(0);
    for (const template of BRAND_CONFIG.greetingTemplates) {
      expect(typeof template).toBe('string');
    }
  });

  it('exposes fallbackGreetings as a non-empty array of strings', () => {
    expect(Array.isArray(BRAND_CONFIG.fallbackGreetings)).toBe(true);
    expect(BRAND_CONFIG.fallbackGreetings.length).toBeGreaterThan(0);
    for (const fallback of BRAND_CONFIG.fallbackGreetings) {
      expect(typeof fallback).toBe('string');
    }
  });

  it('exposes colors.primary/secondary/tertiary as valid hex strings', () => {
    expect(BRAND_CONFIG.colors.primary).toMatch(HEX_COLOR_REGEX);
    expect(BRAND_CONFIG.colors.secondary).toMatch(HEX_COLOR_REGEX);
    expect(BRAND_CONFIG.colors.tertiary).toMatch(HEX_COLOR_REGEX);
  });
});
