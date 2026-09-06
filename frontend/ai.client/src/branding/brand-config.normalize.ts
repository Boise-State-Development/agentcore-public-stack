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

import type { BrandColors, BrandConfig, BrandConfigError, BrandLogoAssets, BrandSurfaces } from './brand.types';
import {
  DEFAULT_ALT_LABEL,
  DEFAULT_COLORS,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_LOGO,
  DEFAULT_PAGE_TITLE,
  DEFAULT_SURFACES,
} from './brand.defaults';
import { HEX_COLOR_REGEX, hexToOklch, normalizeHex } from '../../scripts/branding/color-math';

/** Accepts a 6-digit hex color with an optional leading '#' (Requirement 5.1, 8.3). */
const HEX_COLOR_PATTERN = HEX_COLOR_REGEX;

/**
 * Validation bands for each surface anchor, in OKLCH. `raised` additionally
 * requires lightness >= `light`'s lightness (a cross-field check applied
 * separately in normalizeSurfaces), since cards must sit above the page.
 *
 * Exported so the build-time generators (via `resolveSurfaces` below) and
 * any test asserting on the bands can quote this table rather than
 * re-typing it.
 */
export const SURFACE_BANDS: Record<keyof BrandSurfaces, { minL?: number; maxL?: number; maxC: number }> = {
  light: { minL: 0.9, maxC: 0.04 },
  raised: { minL: 0.95, maxC: 0.03 },
  dark: { maxL: 0.32, maxC: 0.05 },
};

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
  surfaces: BrandSurfaces;
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
 * Normalize a single surface anchor role (`light`, `dark`, or `raised`).
 *
 * Valid: a 6-digit hex value with an optional leading '#' (like
 * `normalizeColorRole`) AND within that role's OKLCH lightness/chroma
 * band (see `SURFACE_BANDS`) — a page background must stay light/dark
 * enough, and low-chroma enough, to keep the rest of the neutral ramp's
 * character intact. Invalid or out-of-band values fall back to the
 * `DEFAULT_SURFACES` hex for that role (never throw), and record a
 * `BrandConfigError` naming the rejection reason.
 */
export function normalizeSurfaceRole(
  role: keyof BrandSurfaces,
  value: unknown,
  errors: BrandConfigError[],
): string {
  if (typeof value !== 'string' || !HEX_COLOR_PATTERN.test(value)) {
    errors.push({
      field: `surfaces.${role}`,
      value: typeof value === 'string' ? value : undefined,
      reason: `surfaces.${role} must be a 6-digit hexadecimal value with an optional leading '#'`,
    });
    return DEFAULT_SURFACES[role];
  }

  const band = SURFACE_BANDS[role];
  const { l, c } = hexToOklch(value);

  if (band.minL !== undefined && l < band.minL) {
    errors.push({
      field: `surfaces.${role}`,
      value,
      reason: `surfaces.${role} must have an OKLCH lightness of at least ${band.minL} (got ${l.toFixed(3)})`,
    });
    return DEFAULT_SURFACES[role];
  }

  if (band.maxL !== undefined && l > band.maxL) {
    errors.push({
      field: `surfaces.${role}`,
      value,
      reason: `surfaces.${role} must have an OKLCH lightness of at most ${band.maxL} (got ${l.toFixed(3)})`,
    });
    return DEFAULT_SURFACES[role];
  }

  if (c > band.maxC) {
    errors.push({
      field: `surfaces.${role}`,
      value,
      reason: `surfaces.${role} must have an OKLCH chroma of at most ${band.maxC} (got ${c.toFixed(3)})`,
    });
    return DEFAULT_SURFACES[role];
  }

  return value;
}

/**
 * Normalize the `surfaces` field (light, dark, raised anchors).
 *
 * Each field is validated independently via `normalizeSurfaceRole` first,
 * then a cross-field check enforces `raised` lightness >= `light`
 * lightness (a card must never sit below the page it floats on). Only
 * `raised` is rejected by the cross-field check, so a bad `light` value
 * doesn't spuriously reject an otherwise-valid `raised` value.
 */
export function normalizeSurfaces(
  surfaces: Partial<BrandSurfaces> | null | undefined,
  errors: BrandConfigError[],
): BrandSurfaces {
  const safeSurfaces = surfaces ?? {};
  const light = normalizeSurfaceRole('light', safeSurfaces.light, errors);
  const dark = normalizeSurfaceRole('dark', safeSurfaces.dark, errors);
  let raised = normalizeSurfaceRole('raised', safeSurfaces.raised, errors);

  if (hexToOklch(raised).l < hexToOklch(light).l) {
    errors.push({
      field: 'surfaces.raised',
      value: raised,
      reason: `surfaces.raised must have an OKLCH lightness >= surfaces.light's lightness (raised: ${hexToOklch(raised).l.toFixed(3)}, light: ${hexToOklch(light).l.toFixed(3)})`,
    });
    raised = DEFAULT_SURFACES.raised;
  }

  return { light, dark, raised };
}

/**
 * Log, for each surface role, whether the configured value was accepted
 * or rejected — so acceptance is exactly as visible in the console as
 * rejection already is via the `BrandConfigError`s pushed by
 * `normalizeSurfaceRole`. Without this, a forker who edits `surfaces` to
 * an in-band value sees no console output at all (no error, because
 * there is nothing to reject), which looks identical to "the edit never
 * ran".
 *
 * Called by each of the three build-time generators' `run()` after
 * resolving surfaces, and takes the already-computed `errors` array so
 * it never re-validates — it just reports what `resolveSurfaces` already
 * decided.
 */
export function logSurfaceAcceptance(
  generatorLabel: string,
  rawSurfaces: Partial<BrandSurfaces> | null | undefined,
  errors: readonly BrandConfigError[],
): void {
  const safeSurfaces = rawSurfaces ?? {};
  const rejectedRoles = new Set(
    errors.filter((e) => e.field.startsWith('surfaces.')).map((e) => e.field.slice('surfaces.'.length)),
  );

  for (const role of ['light', 'dark', 'raised'] as const) {
    if (rejectedRoles.has(role)) {
      // Already logged as a rejection via the error loop above — no
      // separate "accepted" line, so the two states aren't printed for
      // the same role.
      continue;
    }
    const configuredValue = safeSurfaces[role];
    console.log(`[${generatorLabel}] surfaces.${role}: ACCEPTED "${configuredValue}"`);
  }
}

/**
 * Resolve a (possibly partial/invalid) `surfaces` input to normalized-hex
 * anchors, for the build-time generators (`generateSurfaceTheme`,
 * `generateSurfaceColors`, `generateBrandTheme`).
 *
 * This is the one place the build-time path and the runtime path
 * (`BrandingService`, via `normalizeBrandConfig`) share the same
 * hex-format + OKLCH band + `raised >= light` validation — see
 * `normalizeSurfaces` above, which this delegates to unchanged.
 *
 * The one difference from `normalizeSurfaces` itself: each resolved role
 * is passed through `normalizeHex` so it always carries a leading `#`.
 * `normalizeSurfaces` deliberately returns the forker's value verbatim
 * (a bare `f9fafb` stays bare) because the runtime path and its spec
 * depend on that; the generators need a leading `#` because
 * `generateSurfaceRamp`'s zero-diff fast path and `generateWhiteOverride`
 * compare and emit normalized hex.
 */
export function resolveSurfaces(
  surfaces: Partial<BrandSurfaces> | null | undefined,
  errors: BrandConfigError[],
): BrandSurfaces {
  const resolved = normalizeSurfaces(surfaces, errors);
  return {
    light: normalizeHex(resolved.light),
    dark: normalizeHex(resolved.dark),
    raised: normalizeHex(resolved.raised),
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
  const surfaces = normalizeSurfaces(safeConfig.surfaces, errors);

  return { logo, appName, greetingTemplates, fallbackGreetings, colors, pageTitle, surfaces, errors };
}
