# Requirements Document

## Introduction

The `branding-customization` feature made the three brand accent colors (primary, secondary, tertiary) rebrandable from a single `Brand_Config` file. It deliberately left the application's neutral surfaces — page backgrounds, cards, panels, side menus, borders, and body text — out of scope, on the stated rationale that "grays and other neutrals are not part of the themed surface."

That rationale no longer holds. A `Forker` rebranding this application for their own organization needs to set the surface color of the light and dark themes, not just the accents. This feature makes the neutral surface family rebrandable from the same `Brand_Config`, driven by two literal `Base_Color` anchors — one per theme.

An earlier attempt at this failed in a way that defines the shape of this feature. Sixteen Angular component stylesheets each contain `@import "tailwindcss"`, including `app.css`, which is attached to the `app-root` host element. Angular's emulated view encapsulation rewrites Tailwind's emitted theme block to `[_ngcontent-%COMP%]:root, [_nghost-%COMP%] { ... }`. The `[_nghost-…]` half matches the component's own host element, so `app-root` re-declares Tailwind's *default* gray scale and `--color-white` for the entire application subtree. Every descendant inherits those defaults instead of any global override. Only `<body>`, which sits outside `app-root`, escaped — which is why the earlier attempt changed the page background and nothing else, and why the side menus were unaffected.

Brand accent colors work today for the inverse reason: `--color-primary-*` is not a Tailwind default, so it is declared only once, in the global `:root`, and nothing clobbers it. Neutrals are Tailwind defaults, so they are clobbered at every component host that imports Tailwind. Removing that per-component duplication is therefore a prerequisite of this feature, not an optimization.

### Scope

In scope:
1. Two `Base_Color` anchors in `Brand_Config` — one for the light theme, one for the dark theme.
2. A build-time `Neutral_Scale_Generator` that derives a full neutral ramp per theme from those anchors.
3. Removal of the per-component Tailwind theme duplication that prevents any global neutral override from reaching the application.
4. Closure of the known neutral leaks — `slate-*` utilities and hardcoded neutral hex/rgba values — so they follow the `Base_Color`.
5. Contrast guard rails that keep surfaces, borders, and text legible for any accepted `Base_Color` pair.
6. `Rebranding_Documentation` covering the new configuration.

Out of scope (explicit non-goals — see Requirement 11):
- Any admin dashboard or in-app UI for editing base colors.
- Backend persistence of base colors.
- Runtime base color overrides.
- Migrating the ~10,000 existing `gray-*` / `bg-white` call sites to new token names. This feature overrides the existing variables instead.
- Making `state-*` (status) or `vendor-*` / `filetype-*` (category identity) tokens follow the `Base_Color`. Their meaning is fixed by design.

## Glossary

- **Base_Color**: A single hex value provided by a `Forker` that anchors one theme's neutral surface family. There are exactly two: `light` and `dark`.
- **Base_Config**: The `baseColors` field added to the existing `Brand_Config` (`frontend/ai.client/src/branding/brand.config.ts`), holding the two `Base_Color` values.
- **Neutral_Scale**: The eleven derived neutral steps (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950) plus the raised-surface value, expressed as overrides of Tailwind's `--color-gray-{step}` and `--color-white` variables.
- **Neutral_Scale_Generator**: The build-time mechanism, extending the existing `Color_Scale_Generator` at `frontend/ai.client/scripts/branding/generate-brand-theme.ts`, that derives both `Neutral_Scale`s from the two `Base_Color` values.
- **Light_Ramp** / **Dark_Ramp**: The `Neutral_Scale` emitted for the light theme (under `:root`) and the dark theme (under `html.dark`) respectively.
- **Raised_Surface**: The neutral surface used for cards, panels, dropdowns, and inputs that sit above the page. In light mode this is `--color-white`; in dark mode it is `--color-gray-800`.
- **Page_Surface**: The neutral surface used for the application background. In light mode this is `--color-gray-50`; in dark mode it is `--color-gray-900`.
- **Border_Step**: A `Neutral_Scale` step used for borders, rings, and dividers — `gray-200` and `gray-300` in light mode, `gray-600` and `gray-700` in dark mode.
- **Text_Step**: A `Neutral_Scale` step used for body text, secondary text, and icons — `gray-500` through `gray-900` in light mode, `gray-100` through `gray-400` in dark mode.
- **Reference_Ladder**: The lightness and chroma values of Tailwind v4's default `gray` palette, which define the neutral relationships the application currently depends on.
- **Component_Theme_Duplication**: The condition in which an Angular component stylesheet contains `@import "tailwindcss"`, causing Tailwind's full theme variable block to be emitted onto that component's host element via `[_nghost-%COMP%]`, shadowing the global values for the component's entire subtree.
- **Neutral_Leak**: A place in the application that renders a neutral color without reading it from a `Neutral_Scale` variable — a `slate-*` / `zinc-*` / `neutral-*` / `stone-*` utility, a hardcoded hex or `rgba()` value, or the `white` CSS keyword.
- **Default_Base_Colors**: The shipped `Base_Color` values, chosen to reproduce the application's current appearance.
- **Forker**: A developer who clones or forks the repository to deploy a rebranded instance.

## Requirements

### Requirement 1: Base Color Configuration

**User Story:** As a Forker, I want to set my light and dark theme surface colors in the same file where I set my brand colors, so that rebranding remains a single-file edit.

#### Acceptance Criteria

1. THE Base_Config SHALL define exactly two Base_Color values, one named `light` and one named `dark`, within the existing Brand_Config file.
2. THE Base_Config SHALL accept each Base_Color as a 6-digit hexadecimal value with an optional leading `#`, case-insensitive, matching the format already accepted for Brand_Color values.
3. THE Base_Config SHALL be the only file a Forker edits to change the application's base colors.
4. IF a Base_Color value is not a valid 6-digit hexadecimal value, THEN THE Neutral_Scale_Generator SHALL use the corresponding Default_Base_Colors value and record an error identifying the offending value and the role.
5. IF the `baseColors` field is absent from the Brand_Config entirely, THEN THE Neutral_Scale_Generator SHALL use both Default_Base_Colors values and record an error.
6. THE Base_Config SHALL resolve at build time, consistent with how Brand_Color values already resolve.

### Requirement 2: Global Cascade Reach

**User Story:** As a Forker, I want my base color to apply to every part of the application, so that the side menus, chat surface, and dialogs match the pages that already respond.

#### Acceptance Criteria

1. THE application SHALL contain Component_Theme_Duplication in exactly zero Angular component stylesheets.
2. WHERE a component stylesheet requires Tailwind theme values, custom variants, or theme functions, THE component stylesheet SHALL obtain them without emitting any CSS output.
3. WHEN the Neutral_Scale is overridden globally, THE override SHALL take effect in the sidenav, the top navigation, the session list, the chat container, the chat input, the message list, the login page, the first-boot page, the admin pages, and every dialog and overlay.
4. THE application SHALL emit each Tailwind utility class exactly once, from the global stylesheet, with no per-component duplicate.
5. WHEN Component_Theme_Duplication is removed, THE rendered appearance of every affected component SHALL be unchanged.
6. THE test suite SHALL fail if any file under `frontend/ai.client/src/` other than `src/styles.css` contains `@import "tailwindcss"`.

### Requirement 3: Neutral Scale Derivation

**User Story:** As a Forker, I want the whole neutral family to follow my base color, so that surfaces, borders, and text read as one coherent palette rather than a tinted background with grey furniture on it.

#### Acceptance Criteria

1. THE Neutral_Scale_Generator SHALL derive a Light_Ramp from the light Base_Color and a Dark_Ramp from the dark Base_Color.
2. THE Neutral_Scale_Generator SHALL emit both ramps as overrides of the same `--color-gray-{step}` and `--color-white` variable names, the Light_Ramp scoped to `:root` and the Dark_Ramp scoped to `html.dark`.
3. THE Light_Ramp SHALL set the Raised_Surface to the light Base_Color exactly.
4. THE Light_Ramp SHALL set the Page_Surface to a lightness that preserves the Reference_Ladder's current separation between Raised_Surface and Page_Surface.
5. THE Dark_Ramp SHALL set the Page_Surface to the dark Base_Color exactly.
6. THE Dark_Ramp SHALL set the Raised_Surface to a lightness that preserves the Reference_Ladder's current separation between Page_Surface and Raised_Surface.
7. WITHIN each ramp, THE Neutral_Scale_Generator SHALL emit steps whose lightness decreases strictly and monotonically from step 50 through step 950.
8. THE Neutral_Scale_Generator SHALL derive each ramp's hue from that ramp's Base_Color.
9. THE Neutral_Scale_Generator SHALL derive each ramp's chroma from that ramp's Base_Color, clamped to a documented ceiling.
10. IF a Base_Color's chroma exceeds the documented ceiling, THEN THE Neutral_Scale_Generator SHALL clamp the ramp's chroma to that ceiling and record a warning identifying the role.
11. WHERE a Base_Color is achromatic, THE Neutral_Scale_Generator SHALL emit that ramp using the Reference_Ladder's own hue and chroma values.
12. THE Neutral_Scale_Generator SHALL leave every Text_Step at the Reference_Ladder's lightness for its theme, so existing text contrast relationships are preserved by construction.
13. THE Neutral_Scale_Generator SHALL emit deterministic output: generating twice from the same Base_Config produces character-for-character identical CSS.
14. WHEN one Base_Color changes, THE Neutral_Scale_Generator SHALL change only that ramp's declarations.

### Requirement 4: Theme Switching

**User Story:** As a user, I want switching between light and dark themes to be instant and complete, so that I never see a half-switched interface.

#### Acceptance Criteria

1. THE application SHALL define both the Light_Ramp and the Dark_Ramp in the stylesheet simultaneously, selected by the presence or absence of the `dark` class on the document root element.
2. WHEN the active theme changes, THE application SHALL apply the corresponding ramp to every element within 1 second and without a full page reload.
3. WHEN the active theme changes, THE application SHALL leave no element rendering a value from the previously active ramp.
4. THE application SHALL apply the correct ramp on first paint, before the Angular application bootstraps, using the existing pre-bootstrap theme script.
5. THE Neutral_Scale_Generator SHALL emit each ramp at a specificity that no component stylesheet or Tailwind layer can shadow.

### Requirement 5: Surface Separation and Legibility

**User Story:** As a Forker, I want the application to stay readable whatever base color I pick, so that a rebrand cannot silently ship an inaccessible interface.

#### Acceptance Criteria

1. THE Neutral_Scale_Generator SHALL verify that each Border_Step meets a contrast ratio of at least 3:1 against both the Page_Surface and the Raised_Surface of its own theme.
2. THE Neutral_Scale_Generator SHALL verify that each Text_Step used for body text meets a contrast ratio of at least 4.5:1 against both the Page_Surface and the Raised_Surface of its own theme.
3. IF a derived ramp fails a contrast verification, THEN THE Neutral_Scale_Generator SHALL adjust the failing step's lightness toward the Reference_Ladder value until the threshold is met, and record a warning identifying the failing pair and the adjustment applied.
4. IF a derived ramp cannot meet a contrast threshold by lightness adjustment alone, THEN THE Neutral_Scale_Generator SHALL fall back to the Reference_Ladder for that step and record an error identifying the role.
5. THE Neutral_Scale_Generator SHALL preserve the Raised_Surface / Page_Surface lightness separation in both themes, so cards remain distinguishable from the page.
6. THE Neutral_Scale_Generator SHALL emit no step whose lightness falls outside the range 0 to 1 inclusive.

### Requirement 6: Brand Color Interaction

**User Story:** As a Forker, I want my brand accents to stay legible against my chosen dark surface, so that changing the base color does not silently break the accent contrast guarantees.

#### Acceptance Criteria

1. THE Color_Scale_Generator SHALL derive its dark-theme background reference from the dark Base_Color rather than from a hardcoded constant.
2. WHEN the dark Base_Color changes, THE Color_Scale_Generator SHALL recompute every `--color-{role}-accessible-dark` alias against the new dark surface.
3. THE Color_Scale_Generator SHALL continue to derive each `--color-{role}-accessible` alias against the light theme's Raised_Surface.
4. THE Color_Scale_Generator SHALL preserve its existing behavior of adjusting only lightness when deriving accessible aliases, holding the configured hue and chroma.

### Requirement 7: Neutral Leak Closure

**User Story:** As a Forker, I want every neutral surface in the application to follow my base color, so that no component stands out as un-rebranded.

#### Acceptance Criteria

1. THE application SHALL contain zero `slate-*`, `zinc-*`, `neutral-*`, and `stone-*` Tailwind utilities under `frontend/ai.client/src/app/`.
2. THE application SHALL replace each removed non-`gray` neutral utility with the corresponding `gray-*` utility at the same Reference_Ladder step.
3. THE application SHALL replace each hardcoded neutral hex value in component styles with the corresponding Neutral_Scale variable reference.
4. THE application SHALL replace each use of the `white` CSS keyword that denotes a neutral surface or neutral text with the corresponding Neutral_Scale variable reference.
5. WHERE a hardcoded neutral appears in a resolved-color context that cannot read a CSS variable, THE application SHALL resolve that value from the computed Neutral_Scale at runtime.
6. THE test suite SHALL fail if a `slate-*`, `zinc-*`, `neutral-*`, or `stone-*` utility is reintroduced under `frontend/ai.client/src/app/`.
7. WHERE a hardcoded neutral is intentionally retained, THE application SHALL document the reason at the call site.

### Requirement 8: Preserve the Default Appearance

**User Story:** As a maintainer, I want the shipped default to look exactly as it does today, so that this feature is provably a no-op until someone changes a base color.

#### Acceptance Criteria

1. THE Default_Base_Colors SHALL be chosen so the derived ramps reproduce the Reference_Ladder.
2. WHEN the Base_Config holds the Default_Base_Colors, THE Light_Ramp and Dark_Ramp SHALL each match the Reference_Ladder within a documented per-channel tolerance in OKLCH lightness, chroma, and hue.
3. WHEN the Base_Config holds the Default_Base_Colors, THE contrast ratio of every verified surface-and-foreground pair SHALL match its current value within a documented tolerance.
4. THE existing golden regression test for the brand `@theme` block SHALL continue to pass character-for-character under Default_Base_Colors.
5. THE test suite SHALL assert the Default_Base_Colors values explicitly, so a change to them is a deliberate, reviewed edit.

### Requirement 9: Validation and Fallbacks

**User Story:** As a Forker, I want a mistake in my base color to produce a clear warning rather than a broken build or an unreadable application.

#### Acceptance Criteria

1. THE Neutral_Scale_Generator SHALL never fail the build because of an invalid, out-of-range, or over-saturated Base_Color.
2. WHEN the Neutral_Scale_Generator records an error or warning, THE Neutral_Scale_Generator SHALL emit it to the build console identifying the field, the offending value, and the reason.
3. THE branding access boundary SHALL normalize the `baseColors` field using the same per-field, independently-defaulting approach already applied to `colors`.
4. WHERE one Base_Color is invalid, THE Neutral_Scale_Generator SHALL still honor the other.

### Requirement 10: Documentation

**User Story:** As a Forker, I want the rebranding guide to tell me how to set my base colors and what to expect, so that I do not have to read the generator source.

#### Acceptance Criteria

1. THE Rebranding_Documentation SHALL describe how to set each Base_Color, naming the field and the accepted hex format.
2. THE Rebranding_Documentation SHALL describe which parts of the interface each Base_Color controls.
3. THE Rebranding_Documentation SHALL state that the Neutral_Scale regenerates automatically at build time via the existing `prebuild` and `prestart` scripts.
4. THE Rebranding_Documentation SHALL state the chroma ceiling and describe what happens when a Base_Color exceeds it.
5. THE Rebranding_Documentation SHALL describe the contrast guard rails and what a Forker sees when one triggers.
6. THE Rebranding_Documentation SHALL provide observable verification steps for both themes.
7. THE Rebranding_Documentation SHALL state that `text-white` in the light theme carries the light Base_Color's tint, and describe the documented follow-on option for separating them.
8. THE steering guidance that currently states neutrals are not part of the themed surface SHALL be corrected.
9. THE steering guidance SHALL state that `gray`, `white`, and `black` are the base tokens and remain permitted, while `slate`, `zinc`, `neutral`, and `stone` are prohibited.

### Requirement 11: Non-Goals

**User Story:** As a maintainer, I want the boundaries of this feature stated explicitly, so that scope is not assumed to include capabilities that were deliberately deferred.

#### Acceptance Criteria

1. THE Rebranding_Documentation SHALL state that an in-app admin UI for editing base colors is out of scope and deferred.
2. THE Rebranding_Documentation SHALL state that backend persistence of base colors is out of scope.
3. THE Rebranding_Documentation SHALL state that runtime base color overrides are out of scope.
4. THE Rebranding_Documentation SHALL state that migrating existing `gray-*` and `bg-white` call sites to new token names is out of scope, and that this feature overrides the existing variables instead.
5. THE Rebranding_Documentation SHALL state that status and category identity tokens do not follow the Base_Color, and why.
