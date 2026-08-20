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

export interface BrandConfig {
  /** Light/dark logo file references (Requirement 2.1). */
  logo: BrandLogoAssets;
  /** App name / logo alt text, 1–100 chars, >=1 non-whitespace (Requirement 3.1). */
  appName: string;
  /** Ordered greeting templates, 1–50 entries, each 1–500 chars (Requirement 4.1). */
  greetingTemplates: string[];
  /** Ordered fallback greetings, 1–50 entries, each 1–500 chars (Requirement 4.2). */
  fallbackGreetings: string[];
  /** Brand colors as single hex inputs (Requirement 5.1, 8.3). */
  colors: BrandColors;
  /** Browser page title (Requirement 7.1). */
  pageTitle: string;
}

export interface BrandConfigError {
  /** Which field was invalid, e.g. 'colors.primary', 'appName'. */
  field: string;
  /** The offending value (for surfacing/identification), if representable. */
  value?: string;
  /** Human-readable reason. */
  reason: string;
}
