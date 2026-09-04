// greeting.provider.spec.ts
//
// Property-based tests for GreetingProvider (src/branding/greeting.provider.ts).
// This file is structured to be extended by later tasks (5.3: Property 7 - fallback
// greeting selection, 5.4: Property 8 - ultimate default greeting) as separate
// `describe` blocks alongside Property 6 below. See design.md "Correctness Properties"
// for the authoritative property text and requirements.md 4.3/4.5 for the acceptance
// criteria validated here.
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fc from 'fast-check';

import { GreetingProvider } from './greeting.provider';
import { BrandingService } from './branding.service';

/**
 * Arbitrary for a first name containing at least one non-whitespace character.
 * Includes names that themselves contain the literal `{name}` token as a
 * stress case for the substitution logic.
 */
const arbFirstName = fc
  .string({ minLength: 1, maxLength: 30 })
  .filter((s) => /\S/.test(s));

/**
 * Arbitrary for a single greeting template: a base string with 0-3 `{name}`
 * tokens randomly inserted, exercising templates with zero, one, or several
 * `{name}` occurrences.
 */
const arbTemplate = fc
  .tuple(
    fc.array(fc.string({ minLength: 0, maxLength: 15 }), { minLength: 1, maxLength: 4 }),
    fc.integer({ min: 0, max: 3 }),
  )
  .map(([parts, nameCount]) => {
    const tokens = [...parts];
    for (let i = 0; i < nameCount; i++) {
      tokens.splice(Math.min(i, tokens.length), 0, '{name}');
    }
    const template = tokens.join(' ');
    // Ensure the template is non-empty even if all parts were empty strings.
    return template.length > 0 ? template : `greeting-${nameCount}`;
  });

/** Arbitrary for a non-empty list of greeting templates. */
const arbTemplateList = fc.array(arbTemplate, { minLength: 1, maxLength: 10 });

/** Arbitrary for a non-empty list of fallback greetings (irrelevant to this property, but must stay non-empty/plausible). */
const arbFallbackList = fc.array(fc.string({ minLength: 1, maxLength: 20 }), {
  minLength: 1,
  maxLength: 5,
});

/**
 * Mutable mock BrandingService double. `greetingTemplates` /
 * `fallbackGreetings` are reassigned between property iterations so a
 * single TestBed-injected GreetingProvider instance can be reused across
 * all fc.assert runs — re-configuring TestBed's module on every property
 * iteration is unsupported and throws internally.
 */
interface MockBrandingService {
  logo: { light: string; dark: string };
  appName: string;
  greetingTemplates: readonly string[];
  fallbackGreetings: readonly string[];
  configErrors: readonly unknown[];
}

describe('GreetingProvider', () => {
  let mockBrandingService: MockBrandingService;
  let provider: GreetingProvider;

  beforeEach(() => {
    mockBrandingService = {
      logo: { light: 'img/logo-light.png', dark: 'img/logo-dark.png' },
      appName: 'Test App',
      greetingTemplates: [],
      fallbackGreetings: [],
      configErrors: [],
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [GreetingProvider, { provide: BrandingService, useValue: mockBrandingService }],
    });
    provider = TestBed.inject(GreetingProvider);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('Property 6: Named greeting substitution', () => {
    // Feature: branding-customization, Property 6: Named greeting substitution
    // Validates: Requirements 4.3, 4.5
    it('resolves to a configured template with every {name} replaced by the first name, and no {name} remaining', () => {
      fc.assert(
        fc.property(arbFirstName, arbTemplateList, arbFallbackList, (firstName, templates, fallbacks) => {
          mockBrandingService.greetingTemplates = templates;
          mockBrandingService.fallbackGreetings = fallbacks;

          const result = provider.resolveGreeting(firstName);

          // No remaining `{name}` placeholder in the result.
          expect(result.includes('{name}')).toBe(false);

          // The result equals some configured template with every `{name}`
          // occurrence replaced by the first name.
          // Use a replacement function so `$` sequences in the name are
          // inserted literally (mirrors GreetingProvider) rather than being
          // interpreted as `replaceAll` special patterns.
          const matchesSomeTemplate = templates.some(
            (template) => template.replaceAll('{name}', () => firstName) === result,
          );
          expect(matchesSomeTemplate).toBe(true);
        }),
        { numRuns: 100 },
      );
    });
  });
});
