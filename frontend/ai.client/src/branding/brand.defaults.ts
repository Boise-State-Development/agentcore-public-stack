/**
 * Default_Branding constants.
 *
 * These are the built-in fallback values used whenever `Brand_Config`
 * (see `brand.config.ts`) is absent, unparseable, or has an invalid/
 * out-of-bounds field for a given slot. They also define the current
 * out-of-the-box appearance of the application, so a clean checkout
 * renders exactly as it did before branding was centralized.
 *
 * All values here are frozen to signal they are immutable defaults.
 * See design.md "Data Models" and "Default_Branding" for details.
 */

import type { BrandColors, BrandLogoAssets } from './brand.types';

/** Default light/dark logo paths (served from /public). */
export const DEFAULT_LOGO: BrandLogoAssets = Object.freeze({
  light: 'img/logo-light.png',
  dark: 'img/logo-dark.png',
});

/** Default app name / logo alt text. */
export const DEFAULT_APP_NAME = 'Boise State Logo';

/** Fixed default label used when appName normalization fails (Requirement 3.5). */
export const DEFAULT_ALT_LABEL = 'Logo';

/**
 * Default greeting templates (use {name} as placeholder for first name).
 * Copied verbatim from the current `greetingTemplates` array in
 * `session.page.ts`.
 */
export const DEFAULT_GREETING_TEMPLATES: readonly string[] = Object.freeze([
  'How can I help you today, {name}?',
  'What would you like to know, {name}?',
  'Ready to assist you, {name}!',
  'What can I do for you, {name}?',
  "Let's get started, {name}!",
]);

/**
 * Default fallback greetings when a user name is not available.
 * Copied verbatim from the current `fallbackGreetings` array in
 * `session.page.ts`.
 */
export const DEFAULT_FALLBACK_GREETINGS: readonly string[] = Object.freeze([
  'How can I help you today?',
  'What would you like to know?',
  'Ready to assist you!',
  'What can I do for you?',
  "Let's get started!",
]);

/** Built-in ultimate-default greeting when templates and fallbacks are both empty (Requirement 4.8). */
export const DEFAULT_GREETING = 'How can I help you today?';

/** Default brand colors (single hex input per role). */
export const DEFAULT_COLORS: BrandColors = Object.freeze({
  primary: '#0033a0',
  secondary: '#d64309',
  tertiary: '#0072ce',
});

/** Default page title. */
export const DEFAULT_PAGE_TITLE = 'AgentCore';
