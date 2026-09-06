/**
 * BrandingService — the single runtime access boundary for branding
 * values (Requirement 8.2).
 *
 * Components must read logo references, the app name, and greeting
 * arrays exclusively through this service — never by importing
 * `BRAND_CONFIG` or `brand.defaults` directly. This keeps the value
 * source swappable (a future "Option 2" runtime writer) without
 * touching consumers.
 *
 * Reads `BRAND_CONFIG` once, behind a defensive import/shape guard, and
 * delegates per-field normalization to `normalizeBrandConfig`
 * (`brand-config.normalize.ts`). The service never throws: any problem
 * reading or normalizing the config degrades to a usable value (falling
 * back to `Default_Branding`) and is recorded in `configErrors`
 * (Requirement 7.5).
 *
 * See design.md "BrandingService" for the authoritative interface.
 */

import { Injectable } from '@angular/core';

import { BRAND_CONFIG } from './brand.config';
import { normalizeBrandConfig } from './brand-config.normalize';
import {
  DEFAULT_APP_NAME,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_LOGO,
  DEFAULT_PAGE_TITLE,
} from './brand.defaults';
import type { BrandConfig, BrandConfigError, BrandLogoAssets } from './brand.types';

/** The normalized values `BrandingService` exposes, read once at construction. */
interface ResolvedBranding {
  logo: BrandLogoAssets;
  appName: string;
  greetingTemplates: readonly string[];
  fallbackGreetings: readonly string[];
  pageTitle: string;
  errors: BrandConfigError[];
}

@Injectable({ providedIn: 'root' })
export class BrandingService {
  /** Normalized, always-usable logo asset references. */
  readonly logo: BrandLogoAssets;
  /** Normalized app name (falls back to a non-empty default label). */
  readonly appName: string;
  /** Normalized greeting template list (>= 1 entry, or empty if none valid). */
  readonly greetingTemplates: readonly string[];
  /** Normalized fallback greeting list (>= 1 entry, or empty if none valid). */
  readonly fallbackGreetings: readonly string[];
  /** Normalized browser page title. */
  readonly pageTitle: string;
  /** Non-fatal problems found while reading Brand_Config (for surfacing/logging). */
  readonly configErrors: readonly BrandConfigError[];

  constructor() {
    const resolved = BrandingService.resolveBranding();
    this.logo = resolved.logo;
    this.appName = resolved.appName;
    this.greetingTemplates = resolved.greetingTemplates;
    this.fallbackGreetings = resolved.fallbackGreetings;
    this.pageTitle = resolved.pageTitle;
    this.configErrors = resolved.errors;

    // Requirement 8.4: surface any recorded config problems to developers
    // (once, at construction time) without affecting end users.
    for (const error of this.configErrors) {
      console.warn(
        `[BrandingService] Invalid branding config for field "${error.field}"` +
          `${error.value !== undefined ? ` (value: ${JSON.stringify(error.value)})` : ''}: ${error.reason}`,
      );
    }
  }

  /**
   * Read and normalize `BRAND_CONFIG` behind a defensive shape guard.
   *
   * This is the single import/shape guard around `BRAND_CONFIG`
   * (Requirement 7.5): if the imported value is absent, empty, or not a
   * plausible `BrandConfig`-shaped object — or normalization otherwise
   * throws for any reason — this falls back to the full
   * `Default_Branding` set and records a single `brandConfig` error,
   * rather than ever letting the service throw.
   */
  private static resolveBranding(): ResolvedBranding {
    try {
      const raw: unknown = BRAND_CONFIG;

      if (raw === null || raw === undefined || typeof raw !== 'object') {
        throw new Error('BRAND_CONFIG is absent or not an object');
      }

      const { logo, appName, greetingTemplates, fallbackGreetings, pageTitle, errors } = normalizeBrandConfig(
        raw as Partial<BrandConfig>,
      );

      return { logo, appName, greetingTemplates, fallbackGreetings, pageTitle, errors };
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      return {
        logo: { light: DEFAULT_LOGO.light, dark: DEFAULT_LOGO.dark },
        appName: DEFAULT_APP_NAME,
        greetingTemplates: DEFAULT_GREETING_TEMPLATES,
        fallbackGreetings: DEFAULT_FALLBACK_GREETINGS,
        pageTitle: DEFAULT_PAGE_TITLE,
        errors: [
          {
            field: 'brandConfig',
            value: undefined,
            reason: `Brand_Config is absent or unparseable; falling back to Default_Branding (${reason})`,
          },
        ],
      };
    }
  }
}
