/**
 * GreetingProvider — resolves the greeting shown in the Chat_Greeting_Block.
 *
 * Encapsulates greeting selection and `{name}` substitution so consumers
 * (currently `session.page.ts`) no longer hardcode greeting arrays or use
 * a first-only `.replace` for the `{name}` placeholder.
 *
 * A single index is chosen once per service instance (i.e. once per
 * session, since this is `providedIn: 'root'`) and reused across calls,
 * matching the existing `selectedGreetingIndex` behavior in
 * `session.page.ts`.
 *
 * Selection rule (see design.md "GreetingProvider"):
 * 1. Non-blank `firstName` + a non-empty pool → pick from the pool at the
 *    selected index (modulo the pool length) and replace every `{name}`
 *    occurrence with `firstName` via `replaceAll`.
 * 2. Else non-empty fallback pool → the fallback entry at the selected index.
 * 3. Else → the built-in `DEFAULT_GREETING` constant (no `{name}`).
 *
 * **The pool is time-aware.** It is the current part of day's greetings
 * followed by the any-time list, drawn together rather than one instead of the
 * other: about half of what a user sees knows whether it is morning, and half
 * works at any hour. The clock is read at resolve time, from the *viewer's*
 * own `Date` — never the server's — so "Good evening" means evening where the
 * person actually is.
 *
 * The part of day is deliberately not reactive. `session.page.ts` resolves
 * this inside a `computed` keyed on the user's name, so the greeting is fixed
 * for the life of the page; a heading that re-wrote itself from "Good
 * afternoon" to "Good evening" under a user mid-conversation would be a
 * novelty, not information.
 */

import { Injectable } from '@angular/core';

import { BrandingService } from './branding.service';
import { DEFAULT_GREETING } from './brand.defaults';
import type { PartOfDay } from './brand.types';

/**
 * The hour each part of day begins, on the viewer's own clock.
 *
 * Night runs from 22:00 to 04:59 — it is the wrap-around bucket, which is why
 * `partOfDayFor` reads these as ordered thresholds rather than ranges.
 */
export const PART_OF_DAY_START_HOUR: Readonly<Record<PartOfDay, number>> = Object.freeze({
  morning: 5,
  afternoon: 12,
  evening: 17,
  night: 22,
});

/** Which part of the day `date` falls in, on that date's own local clock. */
export function partOfDayFor(date: Date): PartOfDay {
  const hour = date.getHours();
  if (hour >= PART_OF_DAY_START_HOUR.night || hour < PART_OF_DAY_START_HOUR.morning) return 'night';
  if (hour >= PART_OF_DAY_START_HOUR.evening) return 'evening';
  if (hour >= PART_OF_DAY_START_HOUR.afternoon) return 'afternoon';
  return 'morning';
}

@Injectable({ providedIn: 'root' })
export class GreetingProvider {
  /** Chosen once per session (service instance) for consistency across calls. */
  private readonly selectedIndex: number;

  constructor(private readonly brandingService: BrandingService) {
    this.selectedIndex = Math.floor(Math.random() * Number.MAX_SAFE_INTEGER);
  }

  /**
   * Resolve the greeting string to display.
   * @param firstName current user's first name, possibly null/blank
   * @param now the clock to read the part of day from; injectable for tests
   */
  resolveGreeting(firstName: string | null | undefined, now: Date = new Date()): string {
    const part = partOfDayFor(now);
    const templates = this.pool(this.brandingService.timeOfDayGreetings, part, this.brandingService.greetingTemplates);
    const fallbacks = this.pool(
      this.brandingService.timeOfDayFallbackGreetings,
      part,
      this.brandingService.fallbackGreetings,
    );

    if (hasNonWhitespaceChar(firstName) && templates.length > 0) {
      const template = templates[this.selectedIndex % templates.length];
      // Use a replacement function so `$` sequences in the name (e.g. `$&`,
      // `$'`, `$$`) are inserted literally rather than interpreted as
      // `replaceAll` special patterns, which would otherwise leave `{name}`
      // in the output.
      const name = firstName as string;
      return template.replaceAll('{name}', () => name);
    }

    if (fallbacks.length > 0) {
      return fallbacks[this.selectedIndex % fallbacks.length];
    }

    return DEFAULT_GREETING;
  }

  /**
   * This hour's greetings ahead of the any-time ones.
   *
   * Optional-chained rather than trusted: `BrandingService` always supplies
   * the record, but this service is also handed doubles by consumers' own
   * tests, and a greeting is not worth a `TypeError` on the empty state.
   */
  private pool(
    byPart: Readonly<Record<PartOfDay, readonly string[]>> | undefined,
    part: PartOfDay,
    anytime: readonly string[],
  ): readonly string[] {
    const timely = byPart?.[part] ?? [];
    return timely.length > 0 ? [...timely, ...anytime] : anytime;
  }
}

/** True when `value` is a string containing at least one non-whitespace character. */
function hasNonWhitespaceChar(value: string | null | undefined): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}
