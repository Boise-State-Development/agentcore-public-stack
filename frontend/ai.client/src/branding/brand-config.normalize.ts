/**
 * Pure normalization/validation helpers for `Brand_Config`.
 *
 * These functions implement the "Validation rules" table in design.md:
 * every field read from a (possibly partial/absent/invalid) `BrandConfig`
 * is normalized to a usable value, falling back to the corresponding
 * `Default_Branding` value when the provided value is missing or out of
 * bounds. Each fallback records a `BrandConfigError` describing what was
 * rejected and why.
 *
 * This module is intentionally free of Angular dependencies (no
 * `@Injectable`, no DI) so it can be unit- and property-tested in
 * isolation. `BrandingService` (task 4.2) is the thin runtime wrapper
 * that calls `normalizeBrandConfig` once behind an import/shape guard.
 *
 * See design.md "BrandingService" and "Validation rules" for the
 * authoritative behavior.
 */

import type { BrandColors, BrandConfig, BrandConfigError, BrandLogoAssets } from './brand.types';
import {
  DEFAULT_ALT_LABEL,
  DEFAULT_COLORS,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_LOGO,
  DEFAULT_PAGE_TITLE,
} from './brand.defaults';

/** Accepts a 6-digit hex color with an optional leading '#' (Requirement 5.1, 8.3). */
const HEX_COLOR_PATTERN = /^#?[0-9a-fA-F]{6}$/;

/** Greeting array bounds shared by `greetingTemplates` and `fallbackGreetings` (Requirement 4.1, 4.2). */
const MIN_GREETING_ENTRIES = 1;
const MAX_GREETING_ENTRIES = 50;
const MIN_GREETING_LENGTH = 1;
const MAX_GREETING_LENGTH = 500;

/** `appName` bounds (Requirement 3.1). */
const MIN_APP_NAME_LENGTH = 1;
const MAX_APP_NAME_LENGTH = 100;

/** The result of normalizing a (possibly partial/invalid) `BrandConfig`. */
export interface NormalizedBrandConfig {
  logo: BrandLogoAssets;
  appName: string;
  greetingTemplates: string[];
  fallbackGreetings: string[];
  colors: BrandColors;
  pageTitle: string;
  errors: BrandConfigError[];
}

/**
 * Normalize `appName` to a usable alt-text/app-name string.
 *
 * Valid: a string of 1-100 characters containing at least one
 * non-whitespace character (Requirement 3.1). Invalid values fall back
 * to `DEFAULT_ALT_LABEL` — per design.md's validation table, a blank or
 * invalid `appName` falls back to the alt-text default, not
 * `DEFAULT_APP_NAME` (Requirement 3.5).
 */
export function normalizeAppName(value: unknown, errors: BrandConfigError[]): string {
  if (
    typeof value === 'string' &&
    value.length >= MIN_APP_NAME_LENGTH &&
    value.length <= MAX_APP_NAME_LENGTH &&
    /\S/.test(value)
  ) {
    return value;
  }

  errors.push({
    field: 'appName',
    value: typeof value === 'string' ? value : undefined,
    reason: 'appName must be a string of 1-100 characters with at least one non-whitespace character',
  });
  return DEFAULT_ALT_LABEL;
}

/**
 * Normalize a single logo path (`logo.light` or `logo.dark`).
 *
 * Valid: any non-empty string. Invalid values fall back to the
 * corresponding `DEFAULT_LOGO` path.
 */
export function normalizeLogoPath(
  field: 'logo.light' | 'logo.dark',
  value: unknown,
  defaultPath: string,
  errors: BrandConfigError[],
): string {
  if (typeof value === 'string' && value.length > 0) {
    return value;
  }

  errors.push({
    field,
    value: typeof value === 'string' ? value : undefined,
    reason: `${field} must be a non-empty string path`,
  });
  return defaultPath;
}

/**
 * Normalize the `logo` field (both light and dark variants).
 */
export function normalizeLogo(
  logo: Partial<BrandLogoAssets> | null | undefined,
  errors: BrandConfigError[],
): BrandLogoAssets {
  const safeLogo = logo ?? {};
  return {
    light: normalizeLogoPath('logo.light', safeLogo.light, DEFAULT_LOGO.light, errors),
    dark: normalizeLogoPath('logo.dark', safeLogo.dark, DEFAULT_LOGO.dark, errors),
  };
}

/**
 * Normalize a greeting array (`greetingTemplates` or `fallbackGreetings`).
 *
 * Valid shape: an array of 1-50 entries, each a string of 1-500
 * characters (Requirement 4.1, 4.2). Invalid entries are dropped; if the
 * array itself is not a valid array (wrong type, absent, or out of the
 * 1-50 length bound) or ends up with zero valid entries after dropping,
 * the corresponding default array is used instead (Requirement 4.7,
 * 4.8).
 */
export function normalizeGreetingList(
  field: 'greetingTemplates' | 'fallbackGreetings',
  value: unknown,
  defaultList: readonly string[],
  errors: BrandConfigError[],
): string[] {
  if (!Array.isArray(value) || value.length < MIN_GREETING_ENTRIES || value.length > MAX_GREETING_ENTRIES) {
    errors.push({
      field,
      value: undefined,
      reason: `${field} must be an array of ${MIN_GREETING_ENTRIES}-${MAX_GREETING_ENTRIES} entries`,
    });
    return [...defaultList];
  }

  const validEntries = value.filter(
    (entry): entry is string =>
      typeof entry === 'string' &&
      entry.length >= MIN_GREETING_LENGTH &&
      entry.length <= MAX_GREETING_LENGTH,
  );

  if (validEntries.length === 0) {
    errors.push({
      field,
      value: undefined,
      reason: `${field} contained no valid entries (each entry must be ${MIN_GREETING_LENGTH}-${MAX_GREETING_LENGTH} characters)`,
    });
    return [...defaultList];
  }

  if (validEntries.length !== value.length) {
    const droppedCount = value.length - validEntries.length;
    errors.push({
      field,
      value: undefined,
      reason: `${field} contained ${droppedCount} invalid ${droppedCount === 1 ? 'entry' : 'entries'} that were dropped`,
    });
  }

  return validEntries;
}

/**
 * Normalize a single brand color role.
 *
 * Valid: a 6-digit hex value with an optional leading '#' (Requirement
 * 5.1, 8.3). Invalid values fall back to the `DEFAULT_COLORS` hex for
 * that role (Requirement 5.7, 8.5).
 */
export function normalizeColorRole(
  role: keyof BrandColors,
  value: unknown,
  errors: BrandConfigError[],
): string {
  if (typeof value === 'string' && HEX_COLOR_PATTERN.test(value)) {
    return value;
  }

  errors.push({
    field: `colors.${role}`,
    value: typeof value === 'string' ? value : undefined,
    reason: `colors.${role} must be a 6-digit hexadecimal value with an optional leading '#'`,
  });
  return DEFAULT_COLORS[role];
}

/**
 * Normalize the `colors` field (primary, secondary, tertiary roles).
 */
export function normalizeColors(
  colors: Partial<BrandColors> | null | undefined,
  errors: BrandConfigError[],
): BrandColors {
  const safeColors = colors ?? {};
  return {
    primary: normalizeColorRole('primary', safeColors.primary, errors),
    secondary: normalizeColorRole('secondary', safeColors.secondary, errors),
    tertiary: normalizeColorRole('tertiary', safeColors.tertiary, errors),
  };
}

/**
 * Normalize `pageTitle` to a usable browser page title.
 *
 * Valid: a non-empty string of 1-200 characters. Invalid values fall back
 * to DEFAULT_PAGE_TITLE.
 */
export function normalizePageTitle(value: unknown, errors: BrandConfigError[]): string {
  if (typeof value === 'string' && value.length > 0 && value.length <= 200 && /\S/.test(value)) {
    return value;
  }

  errors.push({
    field: 'pageTitle',
    value: typeof value === 'string' ? value : undefined,
    reason: 'pageTitle must be a non-empty string of 1-200 characters',
  });
  return DEFAULT_PAGE_TITLE;
}

/**
 * Normalize a (possibly partial, absent, or invalid) `BrandConfig` into a
 * fully usable set of branding values plus the list of `BrandConfigError`s
 * recorded for any defaulted/invalid field.
 *
 * This is the single entry point `BrandingService` (task 4.2) calls after
 * its import/shape guard. Every field is independently normalized, so a
 * problem in one field (e.g. an invalid `colors.primary`) never affects
 * the normalization of any other field.
 */
export function normalizeBrandConfig(config: Partial<BrandConfig> | null | undefined): NormalizedBrandConfig {
  const safeConfig = config ?? {};
  const errors: BrandConfigError[] = [];

  const logo = normalizeLogo(safeConfig.logo, errors);
  const appName = normalizeAppName(safeConfig.appName, errors);
  const greetingTemplates = normalizeGreetingList(
    'greetingTemplates',
    safeConfig.greetingTemplates,
    DEFAULT_GREETING_TEMPLATES,
    errors,
  );
  const fallbackGreetings = normalizeGreetingList(
    'fallbackGreetings',
    safeConfig.fallbackGreetings,
    DEFAULT_FALLBACK_GREETINGS,
    errors,
  );
  const colors = normalizeColors(safeConfig.colors, errors);
  const pageTitle = normalizePageTitle(safeConfig.pageTitle, errors);

  return { logo, appName, greetingTemplates, fallbackGreetings, colors, pageTitle, errors };
}
