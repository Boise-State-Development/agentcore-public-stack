/**
 * Branding type definitions.
 *
 * These types define the shape of the single source of truth for all
 * rebrandable values in the application (logos, app name, greetings,
 * and colors) and the error shape used to report invalid/defaulted
 * config values.
 *
 * See design.md "Data Models" for the authoritative definitions.
 */

/** A 6-digit hex color input, with optional leading '#'. Validated at read time. */
export type HexColorInput = string;

export interface BrandLogoAssets {
  /** Documented path to the light-theme logo (served from /public). */
  light: string;
  /** Documented path to the dark-theme logo (served from /public). */
  dark: string;
}

export interface BrandColors {
  primary: HexColorInput; // Default_Branding: #0033a0
  secondary: HexColorInput; // Default_Branding: #d64309
  tertiary: HexColorInput; // Default_Branding: #0072ce
}

/**
 * The three configurable surface anchors that drive the app's neutral
 * (`--color-gray-*` / `--color-white`) ramp. `light` is the page
 * background, `dark` is the dark-mode page background, and `raised` is
 * the light-mode card/dropdown/dialog surface (emitted as `--color-white`).
 * See `generate-surface-theme.ts` for how these three hex anchors are
 * expanded into the full neutral ramp.
 */
export interface BrandSurfaces {
  light: HexColorInput; // Default_Surfaces: #f9fafb (Tailwind gray-50)
  dark: HexColorInput; // Default_Surfaces: #101828 (Tailwind gray-900)
  raised: HexColorInput; // Default_Surfaces: #ffffff (white)
}

/**
 * The four windows the greeting pool is bucketed into.
 *
 * Boundaries live in `PART_OF_DAY_START_HOUR` (`greeting.provider.ts`) and
 * are read off the viewer's own clock, so a greeting always matches the time
 * where the person actually is.
 */
export type PartOfDay = 'morning' | 'afternoon' | 'evening' | 'night';

/**
 * Greeting pools keyed by part of day. Every key is required, so a bucket can
 * never silently go missing and leave one window of the day unrepresented.
 */
export type TimeOfDayGreetings = Record<PartOfDay, string[]>;

export interface BrandConfig {
  /** Light/dark logo file references (Requirement 2.1). */
  logo: BrandLogoAssets;
  /** App name / logo alt text, 1–100 chars, >=1 non-whitespace (Requirement 3.1). */
  appName: string;
  /** Ordered greeting templates, 1–50 entries, each 1–500 chars (Requirement 4.1). */
  greetingTemplates: string[];
  /** Ordered fallback greetings, 1–50 entries, each 1–500 chars (Requirement 4.2). */
  fallbackGreetings: string[];
  /**
   * Greetings offered only during their own part of the day, pooled *with*
   * `greetingTemplates` rather than instead of it — so a morning visit draws
   * from the morning lines and the any-time lines together.
   *
   * Optional: omit it and the Default_Branding pools apply. A rebrand that
   * replaces `greetingTemplates` should replace this too, or half the
   * greetings on offer will still be the stock ones. Same entry bounds as the
   * lists above, per bucket.
   */
  timeOfDayGreetings?: TimeOfDayGreetings;
  /** Part-of-day counterparts to `fallbackGreetings`, used when no name is known. */
  timeOfDayFallbackGreetings?: TimeOfDayGreetings;
  /** Brand colors as single hex inputs (Requirement 5.1, 8.3). */
  colors: BrandColors;
  /** Browser page title (Requirement 7.1). */
  pageTitle: string;
  /** Light/dark page background and raised-surface anchors (see `BrandSurfaces`). */
  surfaces: BrandSurfaces;
}

export interface BrandConfigError {
  /** Which field was invalid, e.g. 'colors.primary', 'appName'. */
  field: string;
  /** The offending value (for surfacing/identification), if representable. */
  value?: string;
  /** Human-readable reason. */
  reason: string;
}
