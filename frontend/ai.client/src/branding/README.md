# Rebranding Guide

This guide explains how to rebrand this application for your own organization. It is written for a developer who has cloned or forked this repository and wants to deploy a rebranded instance.

All paths below are relative to `frontend/ai.client/` unless stated otherwise.

## 1. Swap the logo files

The application uses two logo images: a light-theme variant and a dark-theme variant. Both are plain static files served from the `public/` directory, so replacing them requires no code changes.

1. Prepare your replacement logo images. Match the existing files' format (PNG) and similar dimensions/aspect ratio so the sidenav and chat greeting layout are not disrupted.
2. Replace the **light-theme** logo at:
   ```
   public/img/logo-light.png
   ```
3. Replace the **dark-theme** logo at:
   ```
   public/img/logo-dark.png
   ```
4. Keep the file names and extensions identical to the originals shown above. `Brand_Config` (see below) points at these exact paths (`img/logo-light.png` and `img/logo-dark.png`, resolved relative to `public/`), so renaming the files would require also editing `Brand_Config`.
5. That's it — no component template or TypeScript file needs to change for a logo swap.

## 2. Edit the Brand_Config values

All non-logo rebrandable values live in one file:

```
src/branding/brand.config.ts
```

**`Brand_Config` (`src/branding/brand.config.ts`) is the single edit location for every non-logo branding value. No other file needs editing to complete rebranding.** Open it and edit the following named values:

1. **`App_Name`** — edit the `appName` field. This string is used as the accessible alt text on every logo image in the sidenav and the chat greeting block, so set it to your organization or product name (e.g. `"Acme Corp Logo"`).
2. **`Greeting_Templates`** — edit the `greetingTemplates` array. Each entry is a greeting shown on a new/empty chat when the current user's first name is known. Use the `{name}` placeholder anywhere you want the user's first name inserted (see [Greeting behavior](#3-greeting-text-and-the-name-placeholder) below).
3. **`Fallback_Greetings`** — edit the `fallbackGreetings` array. Each entry is a greeting shown on a new/empty chat when the current user's first name is not available. These strings should not rely on a name.
4. **`Brand_Color`** — edit the `colors.primary`, `colors.secondary`, and `colors.tertiary` hex values. Each one is a single hex color that drives that role's entire color scale across both light and dark themes (see [Brand colors](#5-brand-colors-and-color-scale-regeneration) below).
5. **`Brand_Surface`** — edit the `surfaces.light`, `surfaces.dark`, and `surfaces.raised` hex values. These drive the app's page background and raised-surface colors in both themes (see [Surface colors](#4-surface-colors-page-background-dark-background-raised-surfaces) below).

### A worked example

If you'd rather start from a filled-in config than an empty template, see:

```
src/branding/example-custom-brand.config.ts
```

This is a complete, valid `Brand_Config` for a fictional rebrand ("Expo Idaho AI" / "Athletic Club Boise"). It shows every field populated together — a custom logo path, app name, greeting arrays, `page_title`, all three brand colors, and in-band `surfaces` that tint the neutral ramp toward the brand hues (a green `light`/`raised` matching `colors.primary`, and a purple `dark` matching `colors.secondary`). Its inline comment on `surfaces` also walks through how to pick a value inside each OKLCH band.

Use it as a reference for the shape and value ranges, then copy the parts you want into `brand.config.ts` (the file the app actually reads). The example file is illustrative only — editing it has no effect on the running app.

## 3. Greeting text and the `{name}` placeholder

Greeting templates support a `{name}` placeholder. At runtime, every occurrence of `{name}` in the selected `Greeting_Template` is replaced with the current user's first name.

- If the current user's first name **is available** (non-empty, not whitespace-only), one entry from `Greeting_Templates` is chosen and every `{name}` occurrence in it is replaced with that first name.
- If the current user's first name **is not available**, a `Fallback_Greetings` entry is shown instead (no substitution is performed, since there is no name to insert).
- If `Greeting_Templates` is empty or unreadable, the fallback chain also applies — a `Fallback_Greetings` entry is shown.
- If both `Greeting_Templates` and `Fallback_Greetings` are empty or unreadable, a built-in default greeting is shown instead (a fixed string containing no `{name}` placeholder), so the chat greeting is never blank.

## 4. Surface colors (page background, dark background, raised surfaces)

Three additional anchors — `surfaces.light`, `surfaces.dark`, and `surfaces.raised` — control the app's neutral (`gray-*`/`white`) surfaces: the light-mode page background, the dark-mode page background, and the light-mode raised surface (cards, dropdowns, dialogs, table zebra-striping). Edit them alongside `colors` in `brand.config.ts`:

```ts
surfaces: {
  light: '#f9fafb',  // Default_Surfaces: page background in light mode
  dark: '#101828',   // Default_Surfaces: page background in dark mode
  raised: '#ffffff', // Default_Surfaces: card/dropdown/dialog surface (emits --color-white)
}
```

**Why this reaches the whole app, not just `body`.** `surfaces.light` and `surfaces.dark` drive a full remapped `--color-gray-*` ramp (all 11 Tailwind steps), and `surfaces.raised` drives `--color-white`. Because `gray-*` utilities and `var(--color-gray-*)` references are used throughout the app's stylesheets — not just the `body` background rule — editing these three hexes reaches every card, sidebar, table, modal, and border, not just the page background. After this feature, `gray-*` in this codebase means "the brand's neutral ramp," not literally Tailwind's gray.

**Validation bands.** Each anchor must be a 6-digit hex (like `Brand_Color`) and fall within an OKLCH lightness/chroma band, so the derived ramp keeps its intended character:

| Anchor | Lightness | Chroma |
| --- | --- | --- |
| `light` | ≥ 0.90 | ≤ 0.04 |
| `raised` | ≥ 0.95 (and ≥ `light`'s lightness) | ≤ 0.03 |
| `dark` | ≤ 0.32 | ≤ 0.05 |

These bands are the reason a bright or saturated color (e.g. a vivid green or purple) is not a valid `surfaces` anchor even though it is a valid 6-digit hex: surfaces are meant to stay in "background" territory (very light or very dark, low chroma) so the derived neutral ramp keeps its non-linear, low-saturation character across all 11 steps. `Brand_Color` has no such band because accent colors are expected to be vivid.

An out-of-band or malformed value falls back to the `Default_Surfaces` hex for that anchor, never throws, and is recorded as a config error (same never-throw contract as `Brand_Color`). This rejection is validated identically wherever `surfaces` is read — the build-time generators (`generate-surface-theme.ts`, `generate-surface-colors.ts`, `generate-brand-theme.ts`) and the runtime `BrandingService` all resolve through the same one validation path, so a `surfaces` edit can never look valid in one place and be silently rejected in another. The rejection is not silent: it is printed in both the `.\start.ps1` startup output (as each generator runs, and again in the final summary block) and the `npm run build` console output, naming the field, the band it violated, and the value that was measured — for example:

```
surfaces.light must have an OKLCH chroma of at most 0.04 (got 0.299)
```

If you edit `surfaces` and nothing on screen changes, check the startup/build console for one of these lines before assuming the feature is broken — the value you wrote was likely out of band and the app fell back to `Default_Surfaces`.

**What the derived ramp does.** Editing `surfaces.light`/`surfaces.dark` does not replace Tailwind's gray ramp with a flat two-color gradient — it applies an OKLCH offset to each of the 11 Tailwind gray steps individually, preserving the ramp's non-linear character (the steps aren't evenly spaced in lightness, and this feature keeps that shape). At the built-in defaults, every declaration is emitted byte-identical to Tailwind's own published gray scale, so a clean checkout renders exactly as it did before this feature existed.

**Accessibility.** After the ramp is derived, text steps that sit on a surface (`text-gray-500/600/700/900` in light mode, `dark:text-gray-200/300/400` in dark mode) are checked against their surface and nudged in lightness only (hue/chroma preserved) if they fail WCAG AA (4.5:1). One known limitation: `text-gray-500` is used in both light mode (as `text-gray-500`) and dark mode (as `dark:text-gray-500`) under opposing constraints — the same variable can't satisfy both simultaneously. It is clamped for the light-mode constraint, and a dark-mode contrast shortfall is recorded as a warning rather than silently ignored or forced to over-correct the light-mode case.

**Code blocks stay fixed.** The Prism syntax-highlighting theme's dark background (`#272822`, in `artifact-source.component.ts`) is intentionally *not* wired to any surface anchor — code editors conventionally stay dark regardless of the surrounding app's light/dark mode, and this codebase follows that convention.

## 5. Brand colors and Color_Scale regeneration

Each `Brand_Color` (`colors.primary`, `colors.secondary`, `colors.tertiary`) is a single hex value. From that single value, a full 11-step derived color scale (steps `50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950`) is generated for that role, where step `500` is your literal hex value and the other steps are lighter/darker variations used throughout the light and dark themes.

Alongside the 11 steps, each role also gets two contrast-checked variants — `{role}-accessible` and `{role}-accessible-dark` — which are guaranteed to be legible with white text and on dark surfaces respectively. See [section 6](#6-using-colors-in-components) for when to use them.

- **When regeneration happens:** editing a `Brand_Color` hex value in `brand.config.ts` regenerates the full derived `Color_Scale` for that role at build time. This happens automatically via the `prebuild` and `prestart` npm scripts, which run the Color_Scale_Generator (`scripts/branding/generate-brand-theme.ts`) before `npm run build` and `npm start` respectively — you do not need to run anything manually. `Brand_Surface` regeneration (the Surface_Ramp_Generator, `scripts/branding/generate-surface-theme.ts`, plus the Chart.js chrome generator, `scripts/branding/generate-surface-colors.ts`) runs from the same two npm scripts, and also from `.\start.ps1`, which runs every generator `prestart` declares before starting the dev server (rather than a single hardcoded one) so a local `.\start.ps1` run and `npm start` never disagree about which generators ran.
- **Accepted hex format:** a `Brand_Color` value must be a 6-digit hexadecimal color. Digits `0-9` and letters `A-F` are accepted (case-insensitive, so both `#0033A0` and `#0033a0` are valid), and the leading `#` is optional (`0033a0` is also valid).
- If a `Brand_Color` value is not in this format, it is rejected and the previous/default color for that role is used instead, so an invalid value cannot break the build.

## 6. Verify your changes

After editing `brand.config.ts` and/or swapping logo files, verify the changes are live:

1. Run `npm start` to launch the development server (this runs the generators via `prestart` automatically).
2. Open the app and check the **sidenav** (top-left) — your replacement logo should be visible, and its accessible name (hover/inspect the alt text) should match your `App_Name`.
3. Start a **new chat** and check the **chat greeting block** — your replacement logo should appear there too, and the greeting text should reflect your `Greeting_Templates`/`Fallback_Greetings` (with your first name substituted if you're logged in with one).
4. Toggle between **light and dark theme** and confirm:
   - The correct logo variant (`logo-light.png` vs `logo-dark.png`) appears in both the sidenav and the chat greeting block.
   - Your `Brand_Color` values (primary/secondary/tertiary) are reflected in the UI's accent colors in both themes.
   - Your `Brand_Surface` values (light/dark/raised) are reflected in the page background, cards, sidenav, tables, and dialogs in both themes.

If something doesn't look right, double check that you edited `brand.config.ts` (not `brand.defaults.ts`, which only holds the built-in fallback values) and that logo files were saved at the exact paths listed in [section 1](#1-swap-the-logo-files).

## 7. Using colors in components

This section is for developers editing components, not for rebranding. It explains which color to reach for and why.

**Do not use Tailwind's built-in color utilities** (`bg-blue-600`, `text-red-500`, `border-amber-300`, and so on) in application code. They ignore the brand configuration and they hide what the color is supposed to mean. Neutrals (`gray-*`, `white`, `black`) are the exception: use them freely — as of the surfaces feature (see [section 4](#4-surface-colors-page-background-dark-background-raised-surfaces)), `gray-*` and `white` *are* part of the themed surface, remapped from `Brand_Surface` at build time. `black` remains a fixed literal (there is no configurable "black" anchor).

Every color belongs to one of three groups. Pick the group first, then the utility.

### Brand

Accent and interactive colors: buttons, links, selected states, focus rings, active tabs. These follow whatever is configured in `brand.config.ts`.

Utilities: `primary-*`, `secondary-*`, `tertiary-*`. Generated into `src/styles/generated/brand-theme.css`.

| Use case | Utility |
| --- | --- |
| Solid fill with white text | `bg-primary-accessible` plus `hover:brightness-95` |
| Colored text or icon on a light surface | `text-primary-accessible` |
| Colored text or icon on a dark surface | `dark:text-primary-accessible-dark` |
| Decorative tint (badge or panel background) | `bg-primary-50`, `dark:bg-primary-900/30` |
| Focus ring | `ring-primary-accessible/50` |

The two `accessible` variants exist because a numbered step is not safe for arbitrary brand colors. A bright configured color at step 500 or 600 can leave white label text unreadable. The build picks a darker or lighter variant automatically, adjusting only lightness so the configured hue is preserved, and leaves the color untouched when it already has enough contrast.

Two consequences worth knowing:

- **Solid fills need no `dark:` override.** What matters is the contrast between the fill and its white text, which does not change between light and dark mode. Lightening the fill in dark mode would only reduce it.
- **Hover uses a brightness filter, not a darker step.** For a light configured color, the accessible variant can already be darker than step 700, so `hover:bg-primary-700` would brighten the button on hover and lose the contrast guarantee.

Numbered steps are still fine for decorative tints, where nothing needs to stay legible against the color. If text sits on the tint, check it.

### Status

Fixed meanings: error, warning, success, informational notice. These never follow the brand — a red error banner stays red even for an organization whose brand color is red.

Utilities: `state-danger-*`, `state-warning-*`, `state-success-*`, `state-info-*`. Defined in `src/styles/tokens/state.css`.

Note that blue is ambiguous in older code, serving as both the original accent color and the informational color. When replacing a `blue-*` utility, decide which it is: interactive elements become `primary-*`, informational notices become `state-info-*`.

### Category

Fixed identity, where the color's job is to distinguish one thing from another or to match an outside convention. Google's icon stays Google-colored, spreadsheets stay green, PDFs stay red. These never follow the brand either.

Utilities: `vendor-*`, `filetype-*`. Defined in `src/styles/tokens/identity.css`.

Chart series colors are the exception and live in TypeScript rather than CSS, because Chart.js needs a resolved color string rather than a utility class.

### Adding a token

Add status colors to `state.css` and category colors to `identity.css`. Copy the value verbatim from `node_modules/tailwindcss/theme.css` so the change is invisible, keeping it in `oklch()` — converting to hex clamps the color and shifts it on wide-gamut displays. Use a literal value rather than `var(--color-red-600)`, because unused Tailwind variables are dropped from the build and the reference would resolve to nothing. Define only the steps you actually need.

## Non-Goals

This branding foundation is a build-time / deploy-time mechanism only. The following capabilities are explicitly **out of scope** for this feature and are not implemented:

- **An in-app admin UI for editing branding** is out of scope for this feature. It is deferred to a future capability explicitly labeled **"Option 2"**. This feature's configuration shape is designed to be reusable by that future capability, but no such UI exists today.
- **Backend persistence of branding values** is out of scope. Branding values live only in the `Brand_Config` source file checked into the repository; there is no database or API storing them.
- **Runtime logo uploads** are out of scope. Logos must be replaced by swapping files on disk (see [section 1](#1-swap-the-logo-files)) before/during a build; there is no upload mechanism at runtime.
- **Runtime color overrides and runtime branding overrides** are out of scope. Brand colors and surface colors are resolved into CSS at build time by the Color_Scale_Generator and Surface_Ramp_Generator respectively; there is no mechanism to change colors, surfaces, greetings, the app name, or logos while the application is running.
