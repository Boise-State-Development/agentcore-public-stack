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
 * 1. Non-blank `firstName` + non-empty `greetingTemplates` → pick the
 *    template at the selected index (modulo the current list length) and
 *    replace every `{name}` occurrence with `firstName` via `replaceAll`.
 * 2. Else non-empty `fallbackGreetings` → the fallback entry at the
 *    selected index (modulo the current list length).
 * 3. Else → the built-in `DEFAULT_GREETING` constant (no `{name}`).
 */

import { Injectable } from '@angular/core';

import { BrandingService } from './branding.service';
import { DEFAULT_GREETING } from './brand.defaults';

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
   */
  resolveGreeting(firstName: string | null | undefined): string {
    const templates = this.brandingService.greetingTemplates;
    const fallbacks = this.brandingService.fallbackGreetings;

    if (hasNonWhitespaceChar(firstName) && templates.length > 0) {
      const template = templates[this.selectedIndex % templates.length];
      return template.replaceAll('{name}', firstName as string);
    }

    if (fallbacks.length > 0) {
      return fallbacks[this.selectedIndex % fallbacks.length];
    }

    return DEFAULT_GREETING;
  }
}

/** True when `value` is a string containing at least one non-whitespace character. */
function hasNonWhitespaceChar(value: string | null | undefined): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}
