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

import { GreetingProvider, PART_OF_DAY_START_HOUR, partOfDayFor } from './greeting.provider';
import { BrandingService } from './branding.service';
import {
  DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS,
  DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES,
} from './brand.defaults';
import { PARTS_OF_DAY } from './brand-config.normalize';
import type { PartOfDay } from './brand.types';

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


/** A local-clock date at `hour` on a fixed, unremarkable day. */
function at(hour: number): Date {
  return new Date(2026, 0, 15, hour, 30, 0);
}

describe('partOfDayFor', () => {
  it('puts each hour of the clock in exactly one part of the day', () => {
    const seen = new Map<number, PartOfDay>();
    for (let hour = 0; hour < 24; hour++) {
      seen.set(hour, partOfDayFor(at(hour)));
    }

    // Every hour is covered, and night is the wrap-around bucket — the one a
    // range-based implementation gets wrong, because it starts on one calendar
    // day and ends on the next.
    expect(seen.size).toBe(24);
    expect([...new Set(seen.values())].sort()).toEqual([...PARTS_OF_DAY].sort());
    expect(seen.get(23)).toBe('night');
    expect(seen.get(0)).toBe('night');
    expect(seen.get(4)).toBe('night');
  });

  it('changes bucket exactly on the documented boundary hours', () => {
    for (const part of PARTS_OF_DAY) {
      const start = new Date(2026, 0, 15, PART_OF_DAY_START_HOUR[part], 0, 0);
      const momentBefore = new Date(start.valueOf() - 1);

      expect(partOfDayFor(start)).toBe(part);
      // Whatever the previous bucket is, it must not still be this one — an
      // off-by-one here shows up as "Good evening" at half past four.
      expect(partOfDayFor(momentBefore)).not.toBe(part);
    }
  });
});

describe('GreetingProvider — time-of-day pools', () => {
  interface TimedMock {
    logo: { light: string; dark: string };
    appName: string;
    greetingTemplates: readonly string[];
    fallbackGreetings: readonly string[];
    timeOfDayGreetings: Record<PartOfDay, readonly string[]>;
    timeOfDayFallbackGreetings: Record<PartOfDay, readonly string[]>;
    configErrors: readonly unknown[];
  }

  let mock: TimedMock;
  let provider: GreetingProvider;

  /** Buckets whose entries name themselves, so a wrong bucket is obvious. */
  function pools(prefix: string): Record<PartOfDay, readonly string[]> {
    return {
      morning: [`${prefix}-morning, {name}`],
      afternoon: [`${prefix}-afternoon, {name}`],
      evening: [`${prefix}-evening, {name}`],
      night: [`${prefix}-night, {name}`],
    };
  }

  beforeEach(() => {
    mock = {
      logo: { light: 'img/logo-light.png', dark: 'img/logo-dark.png' },
      appName: 'Test App',
      greetingTemplates: ['anytime, {name}'],
      fallbackGreetings: ['anytime'],
      timeOfDayGreetings: pools('named'),
      timeOfDayFallbackGreetings: pools('fallback'),
      configErrors: [],
    };

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [GreetingProvider, { provide: BrandingService, useValue: mock }],
    });
    provider = TestBed.inject(GreetingProvider);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('only ever offers this hour and the any-time list', () => {
    for (const part of PARTS_OF_DAY) {
      const result = provider.resolveGreeting('Ada', at(PART_OF_DAY_START_HOUR[part]));
      // The bucket names are baked into the copy, so drawing from the wrong
      // window of the day is visible rather than merely improbable.
      expect([`named-${part}, Ada`, 'anytime, Ada']).toContain(result);
    }
  });

  it('draws from this hour when there is no any-time list to fall back on', () => {
    mock.greetingTemplates = [];
    expect(provider.resolveGreeting('Ada', at(PART_OF_DAY_START_HOUR.morning))).toBe('named-morning, Ada');
    expect(provider.resolveGreeting('Ada', at(PART_OF_DAY_START_HOUR.night))).toBe('named-night, Ada');
  });

  it('substitutes {name} in a time-of-day line like any other template', () => {
    mock.greetingTemplates = [];
    const result = provider.resolveGreeting('Ada', at(PART_OF_DAY_START_HOUR.evening));
    expect(result).not.toContain('{name}');
    expect(result).toContain('Ada');
  });

  it('uses the time-of-day fallbacks when no name is known', () => {
    mock.fallbackGreetings = [];
    expect(provider.resolveGreeting(null, at(PART_OF_DAY_START_HOUR.afternoon))).toBe(
      'fallback-afternoon, {name}',
    );
  });

  it('says nothing special at an hour a rebrand emptied', () => {
    mock.timeOfDayGreetings = { ...pools('named'), night: [] };
    // An empty bucket is the documented opt-out, not a config error to paper
    // over with defaults.
    expect(provider.resolveGreeting('Ada', at(PART_OF_DAY_START_HOUR.night))).toBe('anytime, Ada');
  });

  it('survives a consumer double that predates the field', () => {
    const legacy = {
      greetingTemplates: ['anytime, {name}'],
      fallbackGreetings: ['anytime'],
    } as unknown as BrandingService;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [GreetingProvider, { provide: BrandingService, useValue: legacy }],
    });

    // Several component specs stub BrandingService by hand. A greeting is not
    // worth a TypeError on the empty state.
    expect(TestBed.inject(GreetingProvider).resolveGreeting('Ada', at(9))).toBe('anytime, Ada');
  });
});

describe('Default_Branding time-of-day pools', () => {
  it('offers something at every hour of the day', () => {
    for (const part of PARTS_OF_DAY) {
      expect(DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES[part].length).toBeGreaterThan(0);
      expect(DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS[part].length).toBeGreaterThan(0);
    }
  });

  it('addresses the user by name in every template, and never in a fallback', () => {
    for (const part of PARTS_OF_DAY) {
      // A template without `{name}` is indistinguishable from a fallback, and
      // a fallback *with* one renders the literal token to a signed-out user.
      for (const template of DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES[part]) {
        expect(template).toContain('{name}');
      }
      for (const fallback of DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS[part]) {
        expect(fallback).not.toContain('{name}');
      }
    }
  });

  it('pairs every named template with a fallback', () => {
    for (const part of PARTS_OF_DAY) {
      expect(DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS[part].length).toBe(
        DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES[part].length,
      );
    }
  });
});
