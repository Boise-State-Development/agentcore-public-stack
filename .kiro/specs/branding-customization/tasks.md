# Implementation Plan: Branding Customization

## Overview

Convert the branding-customization design into a series of incremental, test-driven coding steps for the Angular v21 frontend at `frontend/ai.client/`. The work builds a single `Brand_Config` source of truth, a build-time `Color_Scale_Generator` that emits the Tailwind `@theme` color scales, a runtime `BrandingService` access boundary with defensive defaults, a `GreetingProvider` that fixes the current first-only `{name}` replacement, and the Sidenav / Chat_Greeting_Block template wiring — all guaranteeing byte/character-identical output under `Default_Branding`.

Property-based tests use `fast-check` + Vitest (already project dependencies), minimum 100 iterations each, tagged with `// Feature: branding-customization, Property {number}: {property text}`.

All paths are relative to `frontend/ai.client/`.

## Tasks

- [x] 1. Establish the branding configuration foundation
  - [x] 1.1 Define branding types and error shape
    - Create `src/branding/brand.types.ts` exporting `HexColorInput`, `BrandLogoAssets`, `BrandColors`, `BrandConfig`, and `BrandConfigError` exactly as specified in the Data Models section
    - Each config field is one distinct named slot; each color role is a single hex input
    - _Requirements: 1.2, 8.1, 8.3_

  - [x] 1.2 Create Default_Branding constants
    - Create `src/branding/brand.defaults.ts` with frozen constants: `DEFAULT_LOGO = { light: 'img/logo-light.png', dark: 'img/logo-dark.png' }`, `DEFAULT_APP_NAME = 'Boise State University Logo'`, `DEFAULT_ALT_LABEL = 'Logo'`, `DEFAULT_GREETING_TEMPLATES` and `DEFAULT_FALLBACK_GREETINGS` (copied verbatim from the current arrays in `session.page.ts`), `DEFAULT_GREETING = 'How can I help you today?'`, and `DEFAULT_COLORS = { primary: '#0033a0', secondary: '#d64309', tertiary: '#0072ce' }`
    - _Requirements: 3.5, 4.8, 5.1, 7.1, 7.4_

  - [x] 1.3 Create the single Brand_Config source of truth
    - Create `src/branding/brand.config.ts` exporting a `BrandConfig` constant populated with the `Default_Branding` values so a clean checkout renders exactly as today
    - This is the only file a Forker edits for non-logo values
    - _Requirements: 1.1, 1.2, 2.1, 3.1, 4.1, 4.2, 5.1, 8.1_

  - [x]* 1.4 Write shape/example tests for Brand_Config
    - Assert `Brand_Config` exposes every named field: `logo.light`, `logo.dark`, `appName`, `greetingTemplates`, `fallbackGreetings`, `colors.primary/secondary/tertiary`
    - _Requirements: 1.1, 1.2, 2.1, 8.1_

- [x] 2. Implement the Color_Scale_Generator
  - [x] 2.1 Implement hex validation and scale generation
    - Create `scripts/branding/generate-brand-theme.ts` with `STEPS`, the `LIGHTNESS_DELTA` table, the hex regex `/^#?[0-9a-fA-F]{6}$/`, `generateScale(role, hex)` emitting `--color-{role}-{step}: oklch(from {hex} calc(l {+|-} {|delta|}) c h);` for non-500 steps and `--color-{role}-500: {hex};` for step 500, and `generateBrandTheme(config)` returning `{ css, errors }`
    - Invalid hex is rejected: fall back to the `Default_Branding` hex for that role and record a `BrandConfigError` identifying the offending value and role
    - _Requirements: 5.2, 5.3, 5.4, 5.7, 8.5_

  - [x] 2.2 Write property test for generated scale structure and lightness derivation
    - Single fast-check property test file covering both generator structure properties for an arbitrary valid 6-digit hex (optional leading `#`, mixed case). Required, not optional: the golden regression test (3.2) only pins the three `Default_Branding` hexes, so this property test is what guarantees the contract holds for the arbitrary hex a Forker actually supplies
    - **Property 1: Color scale structure** — the generated scale contains exactly 11 declarations named `--color-{role}-{step}` for steps 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950 in order, and step-500 is the literal input hex, unchanged
    - **Property 2: Lightness derivation holds chroma and hue** — every non-500 step is emitted as `oklch(from {hex} calc(l ±delta) c h)`, holding chroma and hue, and the applied lightness offsets are strictly decreasing from step 50 to step 950
    - **Validates: Requirements 5.2, 5.3, 5.4**
    - fast-check + Vitest, min 100 iterations, tagged with both `// Feature: branding-customization, Property 1: ...` and `// Feature: branding-customization, Property 2: ...`

  - [x]* 2.3 Write property test for generator determinism
    - **Property 3: Generator determinism** — generating twice yields character-for-character identical CSS, and changing one role's hex changes only that role's declarations
    - **Validates: Requirements 5.5**

  - [x]* 2.4 Write property test for invalid hex rejection
    - **Property 4: Invalid hex rejection** — any value that is not a valid 6-digit hex (with/without leading `#`) is rejected, uses the `Default_Branding` hex for the role, and records an error identifying the offending value and role
    - **Validates: Requirements 5.7, 8.5**

- [x] 3. Wire the generator into the build and emit the theme partial
  - [x] 3.1 Generate the theme partial and consume it from styles.css
    - Add a runnable entry point to `generate-brand-theme.ts` that writes `src/styles/generated/brand-theme.css` (an `@theme` block containing only the color scales); move the color-scale declarations out of `styles.css` into that partial while leaving `--font-sans` / `--font-sans--font-feature-settings` in `styles.css`; add `@import` of the generated partial to `styles.css`; wire `prebuild` and `prestart` npm scripts in `package.json` to run the generator; commit the generated file
    - _Requirements: 5.5, 5.6_

  - [x] 3.2 Write golden regression test for default color scales
    - Run the generator with the `Default_Branding` hexes and assert the emitted color block equals the current committed `@theme` color declarations character-for-character
    - _Requirements: 7.2_

- [x] 4. Implement the BrandingService access boundary
  - [x] 4.1 Implement validation/normalization helpers
    - In `src/branding/branding.service.ts` (or a colocated helper), implement per-field normalization: `appName` (1–100 chars, ≥1 non-whitespace → else `DEFAULT_ALT_LABEL`), logo paths (non-empty string → else default path), `greetingTemplates`/`fallbackGreetings` (array, 1–50 entries, each 1–500 chars, drop invalid entries), and `colors.{role}` (hex regex → else default hex); record a `BrandConfigError` for each defaulted field
    - _Requirements: 3.1, 3.5, 4.1, 4.2, 5.7, 7.5, 8.4, 8.5_

  - [x] 4.2 Implement the BrandingService
    - Implement `BrandingService` (`providedIn: 'root'`) that reads `Brand_Config` once behind an import/shape guard (absent/empty/unparseable → full `Default_Branding`), exposes readonly `logo`, `appName`, `greetingTemplates`, `fallbackGreetings`, and `configErrors`; never throws; emits a developer-visible console warning for recorded errors
    - _Requirements: 7.5, 8.2, 8.4_

  - [x]* 4.3 Write property test for config normalization defaults
    - **Property 5: Config normalization supplies usable defaults** — for any config with absent/empty/out-of-bounds/invalid fields (or an unreadable config entirely), every value read through the service is usable (valid provided value, else the `Default_Branding` value), and an error is recorded per defaulted field
    - **Validates: Requirements 3.1, 4.1, 4.2, 7.5, 8.4**

  - [x]* 4.4 Write unit tests for the access boundary
    - Absent/unparseable config → all `Default_Branding` + recorded error; blank `appName` → `DEFAULT_ALT_LABEL`; empty logo path → default path; confirm the value source can change without touching consumers
    - _Requirements: 7.5, 8.2, 8.4_

- [x] 5. Implement the GreetingProvider
  - [x] 5.1 Implement resolveGreeting with replaceAll and the fallback chain
    - Create `src/branding/greeting.provider.ts` (`providedIn: 'root'`) reading templates/fallbacks from `BrandingService`, choosing a random index once per session: non-blank `firstName` + non-empty templates → selected template with **every** `{name}` replaced via `replaceAll` (fixing today's first-only `.replace`); else non-empty fallbacks → a fallback entry; else the built-in `DEFAULT_GREETING` (no `{name}`)
    - _Requirements: 1.7, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 5.2 Write property test for named greeting substitution
    - **Property 6: Named greeting substitution** — for any non-whitespace first name and non-empty template list, the result is a configured template with every `{name}` replaced and no `{name}` remaining
    - **Validates: Requirements 4.3, 4.5**

  - [x] 5.3 Write property test for fallback greeting selection
    - **Property 7: Fallback greeting selection** — for any absent/null/empty/whitespace name, or empty/unreadable template list, the result is a member of the configured fallback list (when non-empty)
    - **Validates: Requirements 1.7, 4.4, 4.7, 7.4**

  - [x] 5.4 Write property test for the ultimate default greeting
    - **Property 8: Ultimate default greeting** — when both template and fallback lists are empty/undefined, the result equals the built-in `DEFAULT_GREETING` and contains no `{name}`
    - **Validates: Requirements 4.8**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Wire branding into the components
  - [x] 7.1 Update Sidenav_Component to consume BrandingService
    - Inject `BrandingService` into the sidenav component; keep the dual-`<img>` `dark:hidden` / `hidden dark:block` swap; bind `[src]` from `branding.logo.light`/`branding.logo.dark` and `[alt]` from `branding.appName`; add `(error)="onLogoError($event)"`; implement `onLogoError` to reveal a same-dimension placeholder with a visible "logo failed to load" indication without collapsing layout
    - _Requirements: 1.3, 2.2, 2.4, 2.5, 2.7, 2.8, 3.2, 3.4_

  - [x] 7.2 Update Chat_Greeting_Block to consume BrandingService
    - In `chat-container.component`, inject `BrandingService`; keep the dual-`<img>` swap on the greeting logo; bind `[src]`/`[alt]` from the service; add the `(error)` handler and same-dimension placeholder
    - _Requirements: 1.3, 2.3, 2.4, 2.5, 2.7, 2.8, 3.3, 3.4_

  - [x] 7.3 Replace the hardcoded greeting logic in session.page.ts with GreetingProvider
    - Remove the hardcoded `greetingTemplates`/`fallbackGreetings` arrays and the inline `.replace`; delegate the `greetingMessage` computed to `GreetingProvider.resolveGreeting(firstName)`
    - _Requirements: 1.3, 4.6_

  - [x]* 7.4 Write property test for logo alt text equals normalized app name
    - **Property 9: Logo alt text equals normalized app name** — component tests driven by generated app names assert every branding logo image in the Sidenav_Component and Chat_Greeting_Block has identical alt text equal to the normalized app name (valid `App_Name`, else the fixed default label)
    - **Validates: Requirements 3.2, 3.3, 3.4, 3.5**

  - [x]* 7.5 Write component unit tests for theme swap and error handling
    - Assert the dual-`<img>` visible-logo swap on theme toggle occurs via `dark:` CSS with no navigation/reload; assert `(error)` reveals the same-dimension placeholder and keeps surrounding content rendered
    - _Requirements: 2.4, 2.5, 2.6, 2.8, 7.3_

- [x] 8. Guard the default appearance with golden regression tests
  - [x]* 8.1 Write golden logo-path and greeting regression tests
    - Assert `DEFAULT_LOGO` equals `img/logo-light.png` / `img/logo-dark.png`; assert `DEFAULT_GREETING_TEMPLATES` / `DEFAULT_FALLBACK_GREETINGS` equal the arrays currently defined in `session.page.ts`
    - _Requirements: 7.1, 7.4_

- [x] 9. Author the Rebranding_Documentation
  - [x] 9.1 Write the rebranding guide including the Non-Goals section
    - Create the rebranding docs: ordered logo-swap steps identifying both the light and dark variant paths; ordered edit steps for `App_Name`, `Greeting_Templates`, `Fallback_Greetings`, and `Brand_Color` values (each named); a statement that `Brand_Config` is the single edit location and no other file needs editing; the `{name}` runtime-substitution behavior and the no-first-name result; the hex→scale regeneration behavior and accepted hex format; observable verification steps; and a "Non-Goals" heading stating admin UI ("Option 2"), backend persistence, runtime logo uploads, and runtime color/branding overrides are out of scope
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x]* 9.2 Write doc-presence test for Non-Goals coverage
    - Assert a heading containing "Non-Goals" exists and each deferred capability (admin UI / "Option 2", backend persistence, runtime logo uploads, runtime color/branding overrides) is stated
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 10. Final checkpoint - Ensure all tests pass and verify appearance in both themes
  - Ensure all tests pass, ask the user if questions arise.
  - **MANUAL VERIFICATION (not an automated test)** — run the app and confirm in **both light and dark themes**: the Sidenav_Component logo and the Chat_Greeting_Block logo render the correct theme variant and are visually unchanged from before the refactor; the primary / secondary / tertiary brand colors across the UI are unchanged; and toggling the theme swaps both logos and colors instantly with no page reload. Requirement 7 (preserve existing appearance) is an eyeball check that no automated test in this plan covers.
  - _Requirements: 2.4, 2.5, 2.6, 7.1, 7.2, 7.3_

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP.
- Each task references specific requirements for traceability.
- Property tests (fast-check + Vitest, min 100 iterations) are placed next to the code they validate to catch errors early; each is tagged `// Feature: branding-customization, Property {number}: {property text}`.
- Task 2.2 is a single required property test covering Properties 1 and 2; the remaining generator property tests (2.3, 2.4) stay optional.
- Golden/snapshot tests (3.2, 8.1) are the guard that centralizing branding does not change the current appearance under `Default_Branding`.
- Checkpoints ensure incremental validation. Task 10 additionally includes a manual light/dark visual verification step because Requirement 7 (preserve existing appearance) cannot be fully covered by automated tests.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "2.1", "4.1"] },
    { "id": 3, "tasks": ["1.4", "2.2", "2.3", "2.4", "3.1", "4.2", "9.1"] },
    { "id": 4, "tasks": ["3.2", "4.3", "4.4", "5.1", "8.1", "9.2"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "7.1", "7.2", "7.3"] },
    { "id": 6, "tasks": ["7.4", "7.5"] }
  ]
}
```
