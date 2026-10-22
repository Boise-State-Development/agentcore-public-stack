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

import type { BrandColors, BrandLogoAssets, BrandSurfaces, PartOfDay } from './brand.types';

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
 *
 * **Every greeting in this file has to fit the Chat_Greeting_Block on one
 * line.** That block is a 720px column with 1rem of side padding, a 56px
 * logo and a 1rem gap, which leaves the `text-4xl/tight` heading a 616px
 * text column — and `AnimatedTextComponent` types it out a character at a
 * time, so a line that wraps does it visibly, mid-animation. Every greeting
 * below was checked in a browser against that column with a 12-character
 * first name substituted for `{name}`, and `greeting-line-length.spec.ts`
 * holds the line from here using the font's own advance widths. Keep new
 * greetings short, and prefer a terse line to a clever one that wraps.
 *
 * Originally copied verbatim from the `greetingTemplates` array in
 * `session.page.ts`; the first two entries were shortened to fit the budget.
 */
export const DEFAULT_GREETING_TEMPLATES: readonly string[] = Object.freeze([
  'How can I help, {name}?',
  "What's on your mind, {name}?",
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

/**
 * Default greetings that only make sense during their own part of the day.
 *
 * Pooled *with* `DEFAULT_GREETING_TEMPLATES`, not instead of it: half the draw
 * is a line that knows what time it is, half is a line that works any time.
 * Pooling rather than replacing is what keeps the app from feeling like it has
 * exactly one thing to say each morning.
 *
 * Like every greeting here, these are written to the 616px one-line budget
 * documented on `DEFAULT_GREETING_TEMPLATES`.
 */
export const DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES: Readonly<
  Record<PartOfDay, readonly string[]>
> = Object.freeze({
  morning: Object.freeze([
    'Good morning, {name}!',
    'Bright and early, {name}.',
    "Morning, {name}. What's first?",
    "Coffee's on, {name}.",
    'Fresh start, {name}. Where to?',
  ]),
  afternoon: Object.freeze([
    'Good afternoon, {name}!',
    "Afternoon, {name}. What's up?",
    'Back at it, {name}?',
    "What's next, {name}?",
    'Where were we, {name}?',
  ]),
  evening: Object.freeze([
    'Good evening, {name}!',
    'Need a hand, {name}?',
    'One more thing, {name}?',
    'Still going, {name}?',
    "Evening, {name}. What's left?",
  ]),
  night: Object.freeze([
    'Midnight oil, {name}?',
    'Working late tonight, {name}?',
    'The quiet hours, {name}.',
    'Still up, {name}? Let me help.',
    "Late one, {name}. What's up?",
  ]),
});

/** Part-of-day counterparts to `DEFAULT_FALLBACK_GREETINGS`, used when no name is known. */
export const DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS: Readonly<
  Record<PartOfDay, readonly string[]>
> = Object.freeze({
  morning: Object.freeze([
    'Good morning!',
    'Bright and early.',
    "Morning — what's first today?",
    "Coffee's on. What's first?",
    'Fresh start. Where do we begin?',
  ]),
  afternoon: Object.freeze([
    'Good afternoon!',
    'What are we working on?',
    'Back at it?',
    "What's next on the list?",
    'Where should we pick up?',
  ]),
  evening: Object.freeze([
    'Good evening!',
    'What can I take off your plate?',
    'One more thing before you log off?',
    'Still going? What do you need?',
    'Evening. Where should we start?',
  ]),
  night: Object.freeze([
    'Burning the midnight oil?',
    'Working late tonight?',
    'What are we tackling tonight?',
    'Still up? Let me help.',
    'Late one. Where should we start?',
  ]),
});

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

/**
 * Default surface anchors — the hex round-trips of Tailwind's own
 * gray-50, gray-900, and white, so a clean checkout's derived neutral
 * ramp (see generate-surface-theme.ts) is byte-identical to
 * TAILWIND_GRAY_RAMP and no pixel changes.
 *
 * `light` and `dark` are `hexToOklch`/`oklchToSrgb` round-trips of
 * Tailwind v4's `--color-gray-50` / `--color-gray-900` (not hand-typed
 * hex), so the zero-diff property in generate-surface-theme.spec.ts holds
 * exactly rather than approximately.
 */
export const DEFAULT_SURFACES: BrandSurfaces = Object.freeze({
  light: '#f9fafb',
  dark: '#101828',
  raised: '#ffffff',
});