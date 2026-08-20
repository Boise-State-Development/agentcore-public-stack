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
4. **`Brand_Color`** — edit the `colors.primary`, `colors.secondary`, and `colors.tertiary` hex values. Each one is a single hex color that drives that role's entire color scale across both light and dark themes (see [Brand colors](#4-brand-colors-and-color-scale-regeneration) below).

## 3. Greeting text and the `{name}` placeholder

Greeting templates support a `{name}` placeholder. At runtime, every occurrence of `{name}` in the selected `Greeting_Template` is replaced with the current user's first name.

- If the current user's first name **is available** (non-empty, not whitespace-only), one entry from `Greeting_Templates` is chosen and every `{name}` occurrence in it is replaced with that first name.
- If the current user's first name **is not available**, a `Fallback_Greetings` entry is shown instead (no substitution is performed, since there is no name to insert).
- If `Greeting_Templates` is empty or unreadable, the fallback chain also applies — a `Fallback_Greetings` entry is shown.
- If both `Greeting_Templates` and `Fallback_Greetings` are empty or unreadable, a built-in default greeting is shown instead (a fixed string containing no `{name}` placeholder), so the chat greeting is never blank.

## 4. Brand colors and Color_Scale regeneration

Each `Brand_Color` (`colors.primary`, `colors.secondary`, `colors.tertiary`) is a single hex value. From that single value, a full 11-step derived color scale (steps `50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950`) is generated for that role, where step `500` is your literal hex value and the other steps are lighter/darker variations used throughout the light and dark themes.

Alongside the 11 steps, each role also gets two contrast-checked variants — `{role}-accessible` and `{role}-accessible-dark` — which are guaranteed to be legible with white text and on dark surfaces respectively. See [section 6](#6-using-colors-in-components) for when to use them.

- **When regeneration happens:** editing a `Brand_Color` hex value in `brand.config.ts` regenerates the full derived `Color_Scale` for that role at build time. This happens automatically via the `prebuild` and `prestart` npm scripts, which run the Color_Scale_Generator (`scripts/branding/generate-brand-theme.ts`) before `npm run build` and `npm start` respectively — you do not need to run anything manually.
- **Accepted hex format:** a `Brand_Color` value must be a 6-digit hexadecimal color. Digits `0-9` and letters `A-F` are accepted (case-insensitive, so both `#0033A0` and `#0033a0` are valid), and the leading `#` is optional (`0033a0` is also valid).
- If a `Brand_Color` value is not in this format, it is rejected and the previous/default color for that role is used instead, so an invalid value cannot break the build.

## 5. Verify your changes

After editing `brand.config.ts` and/or swapping logo files, verify the changes are live:

1. Run `npm start` to launch the development server (this runs the generator via `prestart` automatically).
2. Open the app and check the **sidenav** (top-left) — your replacement logo should be visible, and its accessible name (hover/inspect the alt text) should match your `App_Name`.
3. Start a **new chat** and check the **chat greeting block** — your replacement logo should appear there too, and the greeting text should reflect your `Greeting_Templates`/`Fallback_Greetings` (with your first name substituted if you're logged in with one).
4. Toggle between **light and dark theme** and confirm:
   - The correct logo variant (`logo-light.png` vs `logo-dark.png`) appears in both the sidenav and the chat greeting block.
   - Your `Brand_Color` values (primary/secondary/tertiary) are reflected in the UI's accent colors in both themes.

If something doesn't look right, double check that you edited `brand.config.ts` (not `brand.defaults.ts`, which only holds the built-in fallback values) and that logo files were saved at the exact paths listed in [section 1](#1-swap-the-logo-files).

## 6. Using colors in components

This section is for developers editing components, not for rebranding. It explains which color to reach for and why.

**Do not use Tailwind's built-in color utilities** (`bg-blue-600`, `text-red-500`, `border-amber-300`, and so on) in application code. They ignore the brand configuration and they hide what the color is supposed to mean. Grays and other neutrals are fine — they are not part of the themed surface.

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
- **Runtime color overrides and runtime branding overrides** are out of scope. Brand colors are resolved into CSS at build time by the Color_Scale_Generator; there is no mechanism to change colors, greetings, the app name, or logos while the application is running.
