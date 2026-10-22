// greeting-line-length.spec.ts
//
// Every greeting has to fit the Chat_Greeting_Block on ONE line.
//
// The block (`chat-container.component.html`, full-page empty state) is a
// 720px column with 1rem of side padding holding a 56px logo, a 1rem gap and
// the `text-4xl/tight` heading — so the heading gets a 616px text column.
// `AnimatedTextComponent` types the greeting out a character at a time and
// appends a `|` cursor while it runs, so a greeting that overflows does not
// just look cramped: it visibly reflows onto a second line mid-animation, and
// the block below it jumps. Nothing truncates, so the failure mode is always
// the wrap.
//
// jsdom has no font metrics, so this measures with a table of per-character
// advance widths captured from the real `InterVariable` woff2 at
// `600 36px` with the app's `font-feature-settings`, in the app's own styled
// heading. Summed advances track the browser's own layout of these strings to
// within [-5.2px, +7.0px] across 455 name/greeting combinations (kerning is
// what the table cannot see), which MEASUREMENT_TOLERANCE_PX covers.
//
// The stress name is 11 characters. The committed greetings were verified in
// a real browser to hold one line at 12 ("Konstantinos"); the test asks for 11
// plus tolerance so a new greeting has to clear the bar with room to spare
// rather than land on it.
//
// Narrow viewports are deliberately out of scope: below ~720px the column is
// the viewport, and no greeting worth writing fits a phone on one line.
import { describe, it, expect } from 'vitest';

import { BRAND_CONFIG } from './brand.config';
import { BRAND_CONFIG as EXAMPLE_BRAND_CONFIG } from './example-custom-brand.config';
import {
  DEFAULT_GREETING,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES,
  DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS,
} from './brand.defaults';
import type { PartOfDay, TimeOfDayGreetings } from './brand.types';

/** 720px column − 2×1rem padding − 56px logo − 1rem gap. */
const GREETING_TEXT_COLUMN_PX = 616;

/** Longest first name the greetings are required to hold one line for. */
const STRESS_NAME = 'Christopher';

/** Kerning the advance table cannot see; measured worst case was 5.2px. */
const MEASUREMENT_TOLERANCE_PX = 8;

/**
 * Per-character advance widths for InterVariable at `600 36px` with
 * `font-feature-settings: 'cv02','cv03','cv04','cv11'`, measured in the
 * browser against the app's own `<h1>`. Regenerate if the heading's font,
 * weight or size changes.
 */
const ADVANCE_PX: Readonly<Record<string, number>> = Object.freeze({
  ' ': 8.14, '!': 8.56, '"': 16.18, '#': 22.19, $: 23.03, '%': 32.63, '&': 22.83,
  "'": 9.56, '(': 11.54, ')': 11.54, '*': 18.48, '+': 23.25, ',': 8.51, '-': 15.83,
  '.': 8.51, '/': 12.63, ':': 8.51, ';': 8.59, '<': 23.25, '=': 23.25, '>': 23.25,
  '?': 19.91, '@': 35.85, '[': 11.54, '\\': 10.35, ']': 11.54, '^': 16.36, _: 16.85,
  '`': 9.97, '{': 15.16, '|': 11.87, '}': 15.16, '~': 23.25,
  '0': 22.96, '1': 13.7, '2': 21.1, '3': 22.06, '4': 23.18,
  '5': 21.32, '6': 21.24, '7': 20.05, '8': 21.73, '9': 21.24,
  A: 25.35, B: 23.13, C: 26.09, D: 25.1, E: 21.54, F: 20.61, G: 26.42, H: 25.73,
  I: 9, J: 19.91, K: 24.06, L: 19.78, M: 31.74, N: 25.93, O: 26.93, P: 22.49,
  Q: 26.93, R: 23.19, S: 23.03, T: 22.57, U: 25.34, V: 25.49, W: 35.87, X: 24.48,
  Y: 24.27, Z: 22.58,
  a: 21.11, b: 21.13, c: 19.6, d: 21.13, e: 19.91, f: 11.03, g: 21.13, h: 20.61,
  i: 8.37, j: 8.38, k: 19.3, l: 8.37, m: 31.28, n: 20.61, o: 20.52, p: 21.13,
  q: 21.13, r: 13, s: 18.17, t: 11.23, u: 20.61, v: 19.43, w: 28.67, x: 18.97,
  y: 19.43, z: 18.29,
  '—': 36, '–': 18, '‘': 7.72, '’': 7.72, '“': 14.46, '”': 14.29,
});

/** Widest glyph in the table, charged for anything the table does not cover. */
const WIDEST_ADVANCE_PX = Math.max(...Object.values(ADVANCE_PX));

/** The `|` the animation appends while it is still typing. */
const CURSOR_PX = ADVANCE_PX['|'];

function renderedWidthPx(text: string): number {
  let width = CURSOR_PX;
  for (const char of text) width += ADVANCE_PX[char] ?? WIDEST_ADVANCE_PX;
  return width;
}

/** The greeting as a user named `STRESS_NAME` sees it. */
function resolved(greeting: string): string {
  return greeting.replaceAll('{name}', STRESS_NAME);
}

function expectFitsOneLine(greeting: string): void {
  const text = resolved(greeting);
  const width = renderedWidthPx(text);
  expect(
    width + MEASUREMENT_TOLERANCE_PX,
    `"${text}" needs ${width.toFixed(0)}px but the greeting column is ${GREETING_TEXT_COLUMN_PX}px — it will wrap. Shorten it.`,
  ).toBeLessThanOrEqual(GREETING_TEXT_COLUMN_PX);
}

const PARTS_OF_DAY: readonly PartOfDay[] = ['morning', 'afternoon', 'evening', 'night'];

function describeByPart(
  label: string,
  pools: Readonly<Record<PartOfDay, readonly string[]>> | TimeOfDayGreetings | undefined,
): void {
  describe(label, () => {
    for (const part of PARTS_OF_DAY) {
      const pool = pools?.[part] ?? [];
      for (const greeting of pool) {
        it(`${part}: ${greeting}`, () => expectFitsOneLine(greeting));
      }
    }
  });
}

describe('greetings fit the Chat_Greeting_Block on one line', () => {
  it('measures the animation cursor as part of the line', () => {
    // Guard on the premise: an empty greeting is not zero-width on screen,
    // because the cursor is already there before the first character lands.
    expect(renderedWidthPx('')).toBe(CURSOR_PX);
  });

  describe('DEFAULT_GREETING_TEMPLATES', () => {
    for (const greeting of DEFAULT_GREETING_TEMPLATES) {
      it(greeting, () => expectFitsOneLine(greeting));
    }
  });

  describe('DEFAULT_FALLBACK_GREETINGS', () => {
    for (const greeting of DEFAULT_FALLBACK_GREETINGS) {
      it(greeting, () => expectFitsOneLine(greeting));
    }
  });

  describeByPart('DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES', DEFAULT_TIME_OF_DAY_GREETING_TEMPLATES);
  describeByPart('DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS', DEFAULT_TIME_OF_DAY_FALLBACK_GREETINGS);

  it('DEFAULT_GREETING', () => expectFitsOneLine(DEFAULT_GREETING));

  // The live config and the worked example are what a rebrand actually ships
  // and what a Forker copies, so both are held to the same budget.
  describe('BRAND_CONFIG', () => {
    for (const greeting of [...BRAND_CONFIG.greetingTemplates, ...BRAND_CONFIG.fallbackGreetings]) {
      it(greeting, () => expectFitsOneLine(greeting));
    }
  });
  describeByPart('BRAND_CONFIG time-of-day', BRAND_CONFIG.timeOfDayGreetings);
  describeByPart('BRAND_CONFIG time-of-day fallbacks', BRAND_CONFIG.timeOfDayFallbackGreetings);

  describe('example-custom-brand.config', () => {
    for (const greeting of [
      ...EXAMPLE_BRAND_CONFIG.greetingTemplates,
      ...EXAMPLE_BRAND_CONFIG.fallbackGreetings,
    ]) {
      it(greeting, () => expectFitsOneLine(greeting));
    }
  });
  describeByPart('example-custom-brand time-of-day', EXAMPLE_BRAND_CONFIG.timeOfDayGreetings);
  describeByPart(
    'example-custom-brand time-of-day fallbacks',
    EXAMPLE_BRAND_CONFIG.timeOfDayFallbackGreetings,
  );
});
