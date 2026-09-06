// brand.defaults.golden.spec.ts
//
// Golden regression tests for the Default_Branding logo paths and greeting
// text (Requirements 7.1, 7.4). Pins DEFAULT_LOGO to today's committed
// logo asset paths, and DEFAULT_GREETING_TEMPLATES / DEFAULT_FALLBACK_GREETINGS
// to the historical `greetingTemplates` / `fallbackGreetings` arrays that
// used to live in `session.page.ts`.
//
// Those arrays were copied verbatim into brand.defaults.ts in task 1.2, and
// then removed from session.page.ts in task 7.3 once the greeting logic was
// delegated to GreetingProvider / BrandingService. Because the live source
// no longer exists in session.page.ts, this test hardcodes the original
// values directly rather than comparing against session.page.ts at runtime.
// This is the guard that centralizing branding did not change the current
// appearance under Default_Branding. See design.md "Snapshot / golden
// regression tests" and requirements.md Requirement 7 (7.1, 7.4).
import { describe, it, expect } from 'vitest';
import {
  DEFAULT_LOGO,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_SURFACES,
} from './brand.defaults';

describe('Default_Branding golden regression (logo paths + greetings)', () => {
  it('DEFAULT_LOGO matches the current logo asset paths', () => {
    expect(DEFAULT_LOGO).toEqual({
      light: 'img/logo-light.png',
      dark: 'img/logo-dark.png',
    });
  });

  it('DEFAULT_GREETING_TEMPLATES matches the historical session.page.ts greetingTemplates array', () => {
    // Historical `greetingTemplates` array from session.page.ts (removed in
    // task 7.3, superseded by GreetingProvider.resolveGreeting), preserved
    // here as the Default_Branding golden value per Requirement 7.4.
    expect(DEFAULT_GREETING_TEMPLATES).toEqual([
      'How can I help you today, {name}?',
      'What would you like to know, {name}?',
      'Ready to assist you, {name}!',
      'What can I do for you, {name}?',
      "Let's get started, {name}!",
    ]);
  });

  it('DEFAULT_FALLBACK_GREETINGS matches the historical session.page.ts fallbackGreetings array', () => {
    // Historical `fallbackGreetings` array from session.page.ts (removed in
    // task 7.3, superseded by GreetingProvider.resolveGreeting), preserved
    // here as the Default_Branding golden value per Requirement 7.4.
    expect(DEFAULT_FALLBACK_GREETINGS).toEqual([
      'How can I help you today?',
      'What would you like to know?',
      'Ready to assist you!',
      'What can I do for you?',
      "Let's get started!",
    ]);
  });

  it('DEFAULT_SURFACES matches the hex round-trips of Tailwind gray-50, gray-900, and white', () => {
    // Pinned so the surfaces feature's zero-diff property (see
    // generate-surface-theme.spec.ts) has a stable, committed anchor: any
    // accidental edit to these hexes would silently change the derived
    // ramp at default config.
    expect(DEFAULT_SURFACES).toEqual({
      light: '#f9fafb',
      dark: '#101828',
      raised: '#ffffff',
    });
  });
});
