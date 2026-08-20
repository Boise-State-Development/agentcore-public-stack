/**
 * Brand_Config — the single source of truth for all rebrandable values.
 *
 * This is the ONLY file to rebrand the app for non-logo
 * values (app name, greeting text, and brand colors). Logo files
 * themselves are separate, documented assets to replace on disk (see
 * the rebranding documentation) — this file only points at their paths.
 *
 * Populated with the `Default_Branding` values so a clean checkout
 * renders exactly as it does today. Consumers must never import this
 * file directly; they read through `BrandingService`
 * (`src/branding/branding.service.ts`), which normalizes and defends
 * against invalid/missing values.
 */

import type { BrandConfig } from './brand.types';
import {
  DEFAULT_LOGO,
  DEFAULT_APP_NAME,
  DEFAULT_GREETING_TEMPLATES,
  DEFAULT_FALLBACK_GREETINGS,
  DEFAULT_COLORS,
  DEFAULT_PAGE_TITLE,
} from './brand.defaults';

/**
 * The single Brand_Config source of truth.
 *
 * `greetingTemplates` / `fallbackGreetings` are spread into new mutable
 * arrays (the `BrandConfig` interface requires `string[]`, not
 * `readonly string[]`) so a Forker can freely edit these arrays in
 * place without TypeScript complaining about readonly defaults.
 */
export const BRAND_CONFIG: BrandConfig = {
  logo: DEFAULT_LOGO,
  appName: DEFAULT_APP_NAME,
  greetingTemplates: [
    ...DEFAULT_GREETING_TEMPLATES
  ],
  fallbackGreetings: [
    ...DEFAULT_FALLBACK_GREETINGS
  ],
  colors: DEFAULT_COLORS,
  pageTitle: DEFAULT_PAGE_TITLE,
};
