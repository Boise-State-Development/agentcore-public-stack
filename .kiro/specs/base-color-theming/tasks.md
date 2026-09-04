# Implementation Plan: Base Color Theming

## Overview

Convert the base-color-theming design into incremental, verifiable coding steps for the Angular v21 frontend at `frontend/ai.client/`. The work has a strict ordering: the component-stylesheet cascade fix (Phase 0) must land and be verified as pixel-neutral before any color derivation is written, because without it no global neutral override reaches the application and any color bug will be indistinguishable from the cascade bug.

Property tests use `fast-check` + Vitest (already project dependencies), minimum 100 iterations each, tagged `// Feature: base-color-theming, Property {number}: {property text}`.

All paths are relative to `frontend/ai.client/` unless stated otherwise.

## Tasks

- [ ] 1. Unblock the cascade
  - [ ] 1.1 Add the component-stylesheet guard spec first
    - Create `src/styles/no-component-tailwind-import.spec.ts` scanning `src/**/*.css` and asserting `@import "tailwindcss"` appears in `src/styles.css` and nowhere else
    - Write it before the fix so it starts red and turns green, proving it actually detects the condition
    - _Requirements: 2.1, 2.6_

  - [ ] 1.2 Replace the per-component Tailwind imports with `@reference`
    - In each of the sixteen component stylesheets, replace `@import "tailwindcss";` with `@reference "tailwindcss";`: `app/app.css`, `app/components/sidenav/sidenav.css`, `app/components/sidenav/components/session-list/session-list.css`, `app/components/topnav/topnav.css`, `app/components/topnav/components/theme-toggle/theme-toggle.component.css`, `app/session/session.page.css`, `app/session/components/voice-overlay/voice-overlay.component.css`, `app/session/components/message-list/message-list.component.css`, `app/session/components/chat-input/chat-input.component.css`, `app/session/components/chat-container/chat-container.component.css`, `app/auth/login/login.page.css`, `app/auth/first-boot/first-boot.page.css`, `app/admin/gemini-models/gemini-models.page.css`, `app/admin/bedrock-models/bedrock-models.page.css`, `app/agents/migration/agents-migration.page.css`
    - Re-run a directory search first — the list above is from an audit and must be confirmed, not trusted
    - Leave the `@custom-variant dark (…)` declarations in place; they are definitions, not emissions
    - Change nothing else in these files
    - _Requirements: 2.1, 2.2, 2.4_

  - [ ] 1.3 Verify the cascade fix in the build output
    - Run `npm run build`; confirm zero `--color-gray-900:` *definitions* in `dist/**/*.js` (as distinct from `var(--color-gray-900)` references)
    - Confirm `dist/**/styles-*.css` still contains `.bg-slate-800\/70` and `.bg-slate-100`, which appear only inside inline `template:` strings — this proves the global stylesheet is still scanning every source
    - Confirm `sidenav-panel-enter`, `artifact-pane-open`, and `approval-prompt` are still present in the component chunks
    - Record the `anyComponentStyle` budget headroom before and after
    - _Requirements: 2.3, 2.4_

  - [ ] 1.4 Checkpoint — confirm Phase 0 is pixel-neutral
    - **MANUAL VERIFICATION (not an automated test)** — run the app and walk chat, sidenav expanded and collapsed, topnav, session list, model dropdown, chat input focus and drag states, message list with tool approvals and artifacts, login, first-boot, and three admin pages, in **both** themes
    - The diff must be invisible. Any visible difference is a specificity interaction that must be understood and explained before proceeding — do not proceed on the assumption it is cosmetic
    - Run `npm run test:ci` and confirm green
    - Commit Phase 0 on its own so it is independently revertable
    - _Requirements: 2.5_

- [ ] 2. Add the Base_Color configuration surface
  - [ ] 2.1 Define the base color types
    - Add `BrandBaseColors` to `src/branding/brand.types.ts` with `light` and `dark` `HexColorInput` fields, documenting that `light` anchors the light theme's Raised_Surface and `dark` anchors the dark theme's Page_Surface
    - Add `baseColors: BrandBaseColors` to `BrandConfig`
    - _Requirements: 1.1, 1.2_

  - [ ] 2.2 Add the Default_Base_Colors constant
    - Add frozen `DEFAULT_BASE_COLORS` to `src/branding/brand.defaults.ts`
    - Determine the `dark` value empirically: find the hex whose `hexToOklch` result lands closest to `oklch(21% 0.034 264.665)` (Tailwind `gray-900`), start from `#101828`, and record the measured per-channel deviation in a comment
    - _Requirements: 8.1, 8.2_

  - [ ] 2.3 Populate Brand_Config with the base colors
    - Add a `baseColors` block to `src/branding/brand.config.ts` holding the `DEFAULT_BASE_COLORS` values as literals, matching how `colors` is populated
    - _Requirements: 1.1, 1.3_

  - [ ] 2.4 Add base color normalization
    - Add `normalizeBaseColorRole` and `normalizeBaseColors` to `src/branding/brand-config.normalize.ts`, mirroring `normalizeColorRole` / `normalizeColors` exactly — same `HEX_COLOR_PATTERN`, same `BrandConfigError` shape, independent per-role defaulting
    - Add `baseColors` to `NormalizedBrandConfig` and wire it into `normalizeBrandConfig`
    - In `BrandingService.resolveBranding`, destructure and discard `baseColors` so its normalization errors still reach `configErrors` and the developer console
    - _Requirements: 1.4, 1.5, 9.3_

  - [ ]* 2.5 Write unit tests for base color normalization
    - Valid with and without leading `#`, mixed case; invalid string; wrong type; absent field; absent `baseColors` object; one role valid and one invalid
    - Assert `DEFAULT_BASE_COLORS` values explicitly so a change to them is a deliberate reviewed edit
    - Assert `BrandingService.configErrors` surfaces a `baseColors.*` error for an invalid value
    - _Requirements: 1.4, 1.5, 8.5, 9.3, 9.4_

- [ ] 3. Implement the Neutral_Scale_Generator
  - [ ] 3.1 Add the Reference_Ladder and derivation constants
    - In `scripts/branding/generate-brand-theme.ts`, add `REFERENCE_LADDER` with the eleven steps' L/C/H values plus the `white` entry, copied verbatim from `node_modules/tailwindcss/theme.css` and cited in a comment
    - Derive `RAISED_PAGE_DELTA_LIGHT`, `PAGE_RAISED_DELTA_DARK`, and `PAGE_DEEP_DELTA_DARK` from the table rather than hardcoding them
    - Add `NEUTRAL_CHROMA_CEILING = 0.04` and `CHROMA_EPSILON = 0.001` with the rationale from the design's derivation section
    - _Requirements: 3.8, 3.9, 8.1_

  - [ ] 3.2 Implement ramp derivation
    - Implement `generateNeutralRamp(theme, baseHex, warnings)` per the design's Derivation algorithm: constant clamped chroma and base hue for a chromatic base, Reference_Ladder hue and chroma for an achromatic base; light ramp anchoring `white` and compressing steps 50–300; dark ramp anchoring `gray-900`, deriving 800 and 950 by the preserved deltas, and remapping 700–500 to preserve ordering; Text_Steps left at Reference_Ladder lightness in both ramps; `--color-white` pure `#fff` in the dark ramp
    - Clamp all lightness to `[0, 1]`, recording a warning on clamp
    - Emit literal `oklch(L% C H)` with L to 1 decimal as a percentage, C and H to 3 decimals, for stable diffs
    - _Requirements: 3.1, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 5.5, 5.6_

  - [ ] 3.3 Implement the contrast verification pass
    - After deriving each ramp, verify the pair table in the design's post-derivation section using the existing `contrastRatio`
    - On failure, walk the failing step's lightness toward its Reference_Ladder value in `LIGHTNESS_SEARCH_STEP` increments until it passes, recording a warning naming the pair and the adjustment
    - If it still fails at the Reference_Ladder value, emit the Reference_Ladder value and record an error
    - Re-verify monotonicity after any adjustment; if an adjustment would break ordering, fall back that step to the Reference_Ladder and record a warning
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 3.7_

  - [ ] 3.4 Implement generateBaseTheme and wire the entry point
    - Implement `generateBaseTheme(config)` returning `{ css, errors }`, emitting the `:root` Light_Ramp block and the `html.dark` Dark_Ramp block with explanatory header comments
    - Update `run()` to resolve the ramps first, then the brand theme, then write the concatenated output to `src/styles/generated/brand-theme.css`
    - Keep `generateBrandTheme`'s signature and output shape unchanged so the existing golden test stays meaningful
    - Print all errors and warnings to the build console with field, value, and reason; never fail the build
    - Commit the regenerated `brand-theme.css`
    - _Requirements: 3.2, 3.13, 3.14, 4.1, 4.5, 9.1, 9.2_

  - [ ] 3.5 Couple the brand accessible aliases to the resolved base surfaces
    - Replace the hardcoded `DARK_SURFACE_OKLCH` with the resolved Dark_Ramp `gray-900`, and the literal-white light background with the resolved Light_Ramp Raised_Surface
    - Update the stale comment claiming `DARK_SURFACE_OKLCH` is kept in sync with `html.dark body` in `styles.css` — the dependency now runs the other way
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [ ] 3.6 Write property tests for ramp structure, monotonicity, and anchors
    - Single required property test file covering the three structural guarantees for arbitrary valid base hex pairs. Required, not optional — the golden test only pins the default pair, so this is what guarantees the contract for the arbitrary hex a Forker actually supplies
    - **Property 1: Ramp structure** — exactly two blocks, one `:root` and one `html.dark`, each with the eleven `--color-gray-{step}` declarations in ascending order plus `--color-white`
    - **Property 2: Monotonic lightness within each ramp** — lightness decreases strictly from step 50 to 950, and `--color-white` is at least as light as step 50
    - **Property 3: Anchors are exact** — `:root --color-white` resolves to the light base exactly; `html.dark --color-gray-900` resolves to the dark base exactly
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.7, 5.6**

  - [ ] 3.7 Write property test for contrast guarantees
    - **Property 6: Contrast guarantees hold** — every verified foreground/background pair meets its target (4.5:1 text, 3:1 borders) against both the Page_Surface and Raised_Surface of its own theme
    - Required, not optional. This is the only automated defence against a rebrand shipping an unreadable interface
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**

  - [ ]* 3.8 Write property test for surface separation and bounded tint
    - **Property 4: Surface separation is preserved** — light `white`→`gray-50` and dark `gray-900`→`gray-800` separations equal the Reference_Ladder separations, absent a documented boundary clamp
    - **Property 5: Tint derivation is bounded** — every step's chroma is at most the ceiling; hue equals the base hue when chromatic; Reference_Ladder hue and chroma are reproduced when achromatic
    - **Validates: Requirements 3.4, 3.6, 3.8, 3.9, 3.10, 3.11, 5.5**

  - [ ]* 3.9 Write property tests for determinism and safe degradation
    - **Property 7: Generator determinism and independence** — two runs are character-identical; changing one base color changes only that ramp and leaves brand declarations byte-identical
    - **Property 8: Invalid base colors degrade safely** — any non-hex value defaults that role only, records an identifying error, honors the other role, and still emits complete contrast-passing ramps
    - **Validates: Requirements 1.4, 1.5, 3.13, 3.14, 9.1, 9.2, 9.4**

  - [ ]* 3.10 Write property test for brand alias coupling
    - **Property 9: Brand accessible aliases track the base** — `accessible-dark` meets AA against the resolved dark Page_Surface, `accessible` against the resolved light Raised_Surface, with configured hue and chroma held
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**

- [ ] 4. Repair and extend the golden regression tests
  - [ ] 4.1 Fix the existing brand theme golden test
    - `brand-theme-golden.spec.ts`'s `extractThemeDeclarations` uses `lastIndexOf('}')`, which now finds the `html.dark` block's closing brace. Rework it to bound the `@theme { … }` block correctly
    - Confirm the character-for-character brand assertion still passes
    - _Requirements: 8.4_

  - [ ] 4.2 Add the neutral ramp golden test
    - New spec comparing both ramps against the Reference_Ladder **numerically**, not textually: per-channel tolerance ΔL ≤ 0.005, ΔC ≤ 0.002, Δh ≤ 1°
    - Additionally assert every verified contrast ratio is within 0.05 of its current value
    - Document in the file header why this is a tolerance comparison rather than the byte-identity guarantee `branding-customization` uses: a hex Base_Color cannot round-trip exactly to the OKLCH the Tailwind palette is authored in
    - _Requirements: 8.2, 8.3_

- [ ] 5. Checkpoint — ensure all tests pass
  - Ensure all tests pass. Ask the user if questions arise.
  - Regenerate `brand-theme.css` and confirm the committed file is unchanged from what the generator produces

- [ ] 6. Close the Neutral_Leaks
  - [ ] 6.1 Migrate the non-gray neutral utilities
    - Re-run the search for `slate-*`, `zinc-*`, `neutral-*`, `stone-*` under `src/app/` rather than trusting the design's inventory
    - Replace each with the same-numbered `gray-*` utility, preserving any opacity modifier: `dark:bg-slate-800/70` → `dark:bg-gray-800/70`, `text-slate-500` → `text-gray-500`, and so on
    - Expected files: `oauth-consent-prompt`, `tool-approval-prompt`, `message-metadata-badges`, `mcp-app-card`, `mcp-app-consent-prompt`, `chat-input.component.html`, `storage-quota-banner`, `quota-warning-banner`, `model-dropdown`
    - _Requirements: 7.1, 7.2_

  - [ ] 6.2 Migrate hardcoded neutrals in component styles
    - Replace opaque neutral hex values with `var(--color-gray-*)` / `var(--color-white)` references in: `artifact-card.component.ts`, `assistant-indicator.component.ts` (including both `rgb(30 41 59) /* slate-800 */`), `mcp-app-frame.component.ts` shimmer gradient, `file-attachment-badge.component.ts` corner fold
    - Fix `sidenav.css` `.dark nav a.active { color: white }` → `var(--color-white)`
    - Fix `styles.css` `.message-block` `tr:nth-child(odd) td { background-color: white }` → `var(--color-white)`
    - Leave `rgba(0,0,0,…)` shadows and `rgba(255,255,255,…)` dark-mode washes as-is — they compose over the surface beneath them and already follow the base color. Add a brief comment where their intent is not obvious
    - Document and retain the decorative values in `not-found.page.ts` and `artifact-panel.component.ts`
    - _Requirements: 7.3, 7.4, 7.7_

  - [ ] 6.3 Resolve chart neutrals from the computed theme
    - `shared/constants/chart-colors.constants.ts` hardcodes the light and dark `CHART_CHROME` values (tooltip background, title/body text, border, axis text, grid line). Chart.js needs a resolved color string, so replace the literals with a helper that reads the corresponding `--color-*` values from computed style for the active theme
    - Ensure the helper re-reads on theme change so charts do not keep stale colors after a toggle
    - Replace the `'#ffffff'` doughnut segment border in `admin/costs/components/model-breakdown.component.ts` with the resolved Raised_Surface
    - _Requirements: 7.5_

  - [ ] 6.4 Ratchet the color token guard
    - In `src/app/color-tokens.spec.ts`, move `slate`, `zinc`, `neutral`, `stone` from `ALLOWED_NEUTRALS` into `BANNED_PALETTES`; keep `gray`, `white`, `black` allowed
    - Correct the file's explanatory comment, which currently states neutrals "are not part of the themed surface" — `gray` and `white` now are
    - _Requirements: 7.6, 10.9_

  - [ ]* 6.5 Write unit tests for runtime chart color resolution
    - Assert the helper returns the active theme's values and re-reads after the `dark` class is toggled on the document root
    - _Requirements: 7.5_

- [ ] 7. Documentation
  - [ ] 7.1 Extend the rebranding guide
    - Add a "Base colors" section to `src/branding/README.md`: how to set each value, the accepted hex format, which parts of the interface each anchor controls, that the ramps regenerate via the existing `prebuild` / `prestart` scripts, the chroma ceiling and what exceeding it does, the contrast guard rails and what a Forker sees when one triggers, and observable verification steps for both themes
    - State explicitly that `text-white` in the light theme carries the light Base_Color's tint, and describe the follow-on option of splitting `bg-white` onto a separate `surface-raised` token if that is unacceptable for a given brand
    - Extend the existing "Non-Goals" section with: no admin UI for base colors, no backend persistence, no runtime overrides, no migration of existing `gray-*` / `bg-white` call sites, and that status and category identity tokens deliberately do not follow the base color
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 11.1, 11.2, 11.3, 11.4, 11.5_

  - [ ] 7.2 Correct the steering guidance
    - `.kiro/steering/tailwind-colors.md` line 7 states neutrals "are not part of the themed surface." Correct it: `gray`, `white`, and `black` are the base tokens and remain permitted; `slate`, `zinc`, `neutral`, and `stone` are prohibited because they do not follow the Base_Color
    - Apply the same correction to the mirrored guidance in `src/branding/README.md` section 6 and `.kiro/steering/tailwind-theming.md`
    - Add a note to `.kiro/steering/tailwind-theming.md` that component stylesheets must use `@reference "tailwindcss"`, never `@import`, with a one-line explanation of the `[_nghost-…]` shadowing so the next developer does not undo it
    - _Requirements: 10.8, 10.9_

  - [ ]* 7.3 Write doc-presence test for the new Non-Goals entries
    - Assert each newly deferred capability is stated under the Non-Goals heading
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [ ] 8. Final checkpoint — verify both themes and both base configurations
  - Ensure all tests pass. Ask the user if questions arise.
  - **MANUAL VERIFICATION (not an automated test)** — set a deliberately tinted pair (for example light `#FAF7F2`, dark `#1A1614`) and confirm in **both** themes: the tint reaches every surface including the sidenav, topnav, session list, chat input, dialogs, and overlays; cards remain distinguishable from the page; borders and shadows still read; charts and tooltips follow; and toggling the theme switches every element at once with nothing rendering a value from the previous ramp
  - Then restore `DEFAULT_BASE_COLORS`, rebuild, and confirm the application is pixel-identical to `main`. Requirement 8 is an eyeball check that no automated test fully covers
  - Consider tightening the `anyComponentStyle` budget in `angular.json` now that the duplicated theme blocks are gone, so the duplication cannot creep back unnoticed
  - _Requirements: 4.2, 4.3, 4.4, 8.2, 8.3_

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP. Tasks 3.6 and 3.7 are deliberately **not** optional: the golden test only pins the default base pair, so the property tests are the only guarantee the contract holds for an arbitrary Forker-supplied hex, and 3.7 is the only automated defence against shipping an unreadable rebrand.
- Phase 1 must be committed and visually verified separately from everything else. It is the riskiest change in this plan and it must be independently revertable.
- Do not trust the file inventories in the design document. They come from an audit and must be re-derived by search before editing.
- Property tests use `fast-check` + Vitest, minimum 100 iterations, tagged `// Feature: base-color-theming, Property {number}: {property text}`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3"] },
    { "id": 3, "tasks": ["1.4"] },
    { "id": 4, "tasks": ["2.1"] },
    { "id": 5, "tasks": ["2.2", "3.1"] },
    { "id": 6, "tasks": ["2.3", "2.4"] },
    { "id": 7, "tasks": ["2.5", "3.2"] },
    { "id": 8, "tasks": ["3.3"] },
    { "id": 9, "tasks": ["3.4"] },
    { "id": 10, "tasks": ["3.5", "3.6", "3.7", "3.8", "3.9", "4.1", "4.2"] },
    { "id": 11, "tasks": ["3.10", "5"] },
    { "id": 12, "tasks": ["6.1", "6.2", "6.3", "7.1", "7.2"] },
    { "id": 13, "tasks": ["6.4", "6.5", "7.3"] },
    { "id": 14, "tasks": ["8"] }
  ]
}
```
