// branding.service.spec.ts
//
// Unit/example tests for the BrandingService access boundary
// (src/branding/branding.service.ts). Complements the property test in
// 4.3 (config normalization) with concrete example cases and an
// architectural shape assertion.
//
// Note on the absent/unparseable Brand_Config path: BRAND_CONFIG is a
// static module import, and this project's convention (see e.g.
// preview-chat.service.spec.ts, submit-listing-dialog.component.spec.ts)
// is to avoid vi.mock for module mocking because the Angular vitest
// builder's shared worker pool causes module mocks to leak across specs.
// So the "absent/unparseable config" scenario is exercised directly
// against `normalizeBrandConfig(null)` / `normalizeBrandConfig(undefined)`
// — the same normalization core BrandingService's shape guard delegates
// to — while BrandingService itself is exercised against the real
// (valid) BRAND_CONFIG to confirm it instantiates cleanly and exposes the
// expected shape.
// See design.md "BrandingService" / "Error Handling" and requirements.md
// 7.5, 8.2, 8.4.
import { TestBed } from '@angular/core/testing';
import { describe, it, expect } from 'vitest';

import { BrandingService } from './branding.service';
import { normalizeBrandConfig } from './brand-config.normalize';
import {
  DEFAULT_LOGO,
  DEFAULT_ALT_LABEL,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_FALLBACK_GREETINGS,
} from './brand.defaults';

describe('BrandingService', () => {
  it('instantiates without throwing against the real BRAND_CONFIG', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [BrandingService] });

    expect(() => TestBed.inject(BrandingService)).not.toThrow();
  });

  it('exposes the expected shape: logo, appName, greetingTemplates, fallbackGreetings, configErrors', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [BrandingService] });
    const service = TestBed.inject(BrandingService);

    expect(typeof service.logo.light).toBe('string');
    expect(typeof service.logo.dark).toBe('string');
    expect(typeof service.appName).toBe('string');
    expect(Array.isArray(service.greetingTemplates)).toBe(true);
    expect(Array.isArray(service.fallbackGreetings)).toBe(true);
    expect(Array.isArray(service.configErrors)).toBe(true);
  });

  it('reports no config errors when reading the real (valid) BRAND_CONFIG', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [BrandingService] });
    const service = TestBed.inject(BrandingService);

    expect(service.configErrors).toEqual([]);
  });

  it('never exposes BRAND_CONFIG or brand.defaults directly to consumers — only the four normalized properties plus configErrors', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [BrandingService] });
    const service = TestBed.inject(BrandingService);

    const exposedKeys = Object.keys(service).sort();
    expect(exposedKeys).toEqual(['appName', 'configErrors', 'fallbackGreetings', 'greetingTemplates', 'logo'].sort());
  });
});

describe('BrandingService access-boundary fallback behavior (via normalizeBrandConfig)', () => {
  // BRAND_CONFIG is a static import and cannot be swapped for "absent" at
  // test time without vi.mock (avoided per project convention — see file
  // header). This exercises the same normalization core the service's
  // shape guard delegates to for an absent/unparseable config, confirming
  // it falls back to every Default_Branding value and records an error.
  // Note: normalizeBrandConfig treats a missing `appName` field the same
  // as a blank one — per the "Validation rules" table in design.md, a
  // blank/invalid appName falls back to DEFAULT_ALT_LABEL (Requirement
  // 3.5), not DEFAULT_APP_NAME. DEFAULT_APP_NAME is only produced by
  // BrandingService's own shape guard when BRAND_CONFIG itself is not an
  // object at all (the "whole config absent" case), which is not
  // reachable here without vi.mock (see file header).
  it('falls back to all Default_Branding values when the config is null, and records an error', () => {
    const result = normalizeBrandConfig(null);

    expect(result.logo).toEqual(DEFAULT_LOGO);
    expect(result.appName).toBe(DEFAULT_ALT_LABEL);
    expect(result.greetingTemplates).toEqual([...DEFAULT_GREETING_TEMPLATES]);
    expect(result.fallbackGreetings).toEqual([...DEFAULT_FALLBACK_GREETINGS]);
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it('falls back to all Default_Branding values when the config is undefined, and records an error', () => {
    const result = normalizeBrandConfig(undefined);

    expect(result.logo).toEqual(DEFAULT_LOGO);
    expect(result.appName).toBe(DEFAULT_ALT_LABEL);
    expect(result.greetingTemplates).toEqual([...DEFAULT_GREETING_TEMPLATES]);
    expect(result.fallbackGreetings).toEqual([...DEFAULT_FALLBACK_GREETINGS]);
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it('falls back appName to DEFAULT_ALT_LABEL when appName is blank', () => {
    const validConfig = normalizeBrandConfig(undefined);
    const result = normalizeBrandConfig({ ...validConfig, appName: '' });

    expect(result.appName).toBe(DEFAULT_ALT_LABEL);
    expect(result.errors.some((e) => e.field === 'appName')).toBe(true);
  });

  it('falls back logo.light to DEFAULT_LOGO.light when the light logo path is an empty string', () => {
    const validConfig = normalizeBrandConfig(undefined);
    const result = normalizeBrandConfig({
      ...validConfig,
      logo: { light: '', dark: 'valid.png' },
    });

    expect(result.logo.light).toBe(DEFAULT_LOGO.light);
    expect(result.logo.dark).toBe('valid.png');
    expect(result.errors.some((e) => e.field === 'logo.light')).toBe(true);
  });
});
