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
  logo: {
    light: 'img/ac_boise_logo.png',
    dark: 'img/ac_boise_logo.png',
  },
  appName: "Athletic Club Boise Logo",
  greetingTemplates: [
  'How can I help you today, {name}?',
  'What would you like to know, {name}?',
  'Ready to assist you, {name}!',
  'What can I do for you, {name}?',
  "Let's get started, {name}!",
  ],
  fallbackGreetings: [
  'How can I help you today?',
  'What would you like to know?',
  'Ready to assist you!',
  'What can I do for you?',
  "Let's get started!",
  ],
  colors: {
    primary: '#009C46',
    secondary: '#8E3CEB',
    tertiary: '#93FA58',
  },
  pageTitle: "Expo Idaho AI",
  // Surface anchors for the neutral ramp. Each value must fall inside a
  // per-role OKLCH band (see SURFACE_BANDS in brand-config.normalize.ts) or
  // it is rejected and silently reset to the Default_Branding neutral for
  // that role. The bands keep page/card backgrounds near-neutral and legible;
  // only a subtle tint is allowed, not a saturated fill.
  //
  // How to pick a value inside a band:
  //   1. Choose a hue. To tint toward a brand color, reuse its OKLCH hue
  //   2. Hold lightness (L) inside the band:
  //        - light:  L >= 0.90  (a light, near-white page background)
  //        - raised: L >= 0.95  AND L >= light's L (cards sit above the page)
  //        - dark:   L <= 0.32  (a dark page background)
  //   3. Keep chroma (C) at or below the band's ceiling — this is the usual
  //      reason a value is rejected, so stay a touch under it:
  //        - light:  C <= 0.04
  //        - raised: C <= 0.03
  //        - dark:   C <= 0.05
  // Tip: author in oklch(L C H), then convert to hex — or nudge chroma down
  // on an existing hex until it lands in band.
  surfaces: {
    light: '#d7edda',
    dark: '#2c243c',
    raised: '#e5f5e7', // Card background in light mode.
  },
};
