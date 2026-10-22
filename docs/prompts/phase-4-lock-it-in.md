# Task: lock in the color token migration (enforcement + raw hex mopup)

## Context

This is Phase 4, the final phase of the Tailwind color-token migration.
Phases 1–3 are done or nearly done: the `src/app` tree has gone from ~1816
raw `blue-*` usages plus ~2000 other raw palette utilities down to **42
remaining raw palette utility usages**, and the spinner duplication is
consolidated into `<app-spinner>`.

Phase 4 has two jobs:
1. **Enforcement** — make it impossible (or at least loud) to reintroduce raw
   palette utilities, so the migration doesn't silently rot.
2. **Raw hex mopup** — the remaining hardcoded hex color values.

Read first:
- `.kiro/steering/tailwind-colors.md` — the color token rules (authoritative).
- `frontend/ai.client/src/branding/README.md` section 6 — contributor-facing rules.
- `frontend/ai.client/src/app/global-hygiene.spec.ts` — an existing "guard spec"
  in this codebase that enforces a global invariant via a test. This is the
  closest existing precedent for the enforcement mechanism below; read it for
  tone and structure (note how its header comment explains what a failure
  means and what it does *not* mean).
- `docs/prompts/phase-3-outlier-colors-and-icon-spinners.md` — has open
  decisions that overlap this phase's prerequisites. See "Coordination" below.

All paths below are relative to `frontend/ai.client/` unless stated otherwise.

## The one hard rule

Never mix a zero-pixel refactor with a visible recolor in the same chunk.
Phase 4's enforcement work should be **entirely zero-pixel** — adding a guard
does not change any rendered output. The hex mopup in Part 2 may include
recolors; keep those in a separate chunk from the guard, and say which is which.

## Important correction to the original plan

The original 4-phase plan specified "an ESLint rule banning built-in palette
utilities in `src/app`." **This project has no ESLint.** Verified: no
`eslint.config.*`, no `.eslintrc*`, no `eslint`/`angular-eslint`/
`typescript-eslint` in `package.json` devDependencies, and no `lint` script.
(`.kiro/steering/tech.md` claims "Linting: ESLint (frontend)" — that line is
aspirational and currently wrong; consider fixing it as part of this phase.)

Two further facts make a lint rule a poor fit here:

- **137 components use inline `template:` template literals vs. 46 using
  `templateUrl`.** Roughly three quarters of this app's markup lives inside
  TypeScript template literals. `angular-eslint`'s template rules primarily
  target `.html` files; covering inline templates means wiring up its
  processor, and even then the rule would be inspecting template-literal
  contents. Class strings also appear in plain TS (e.g. `computed()` returning
  `'bg-state-danger-600'`, `FILE_TYPE_CONFIG` color maps), which template
  linting wouldn't see at all.
- Adding ESLint means introducing a whole toolchain (eslint + typescript-eslint
  + angular-eslint + flat config + a `lint` script + a CI job) purely to
  enforce one string-pattern rule.

**Recommended instead: a guard spec.** A vitest spec that walks
`src/app/**/*.{ts,html}` and asserts no banned palette utility appears:
- Zero new dependencies.
- Runs in existing CI today — `.github/workflows/tests.yml` already runs
  `npm run test:ci` in `frontend/ai.client`.
- Works uniformly across inline templates, `.html` files, and plain-TS class
  strings, because it reads file text rather than parsing a template AST.
- Matches the existing `global-hygiene.spec.ts` precedent.
- The ratchet (below) is trivial to express as a list of enabled palettes.

The one genuine advantage of ESLint is **editor-time feedback** — red squiggles
as you type, rather than a test failure after the fact. If the user wants that,
the honest answer is "both, eventually": ship the guard spec now because it's
cheap and total, and treat ESLint as an optional later addition for DX. Do not
silently substitute one for the other — state the recommendation, note the
tradeoff, and get agreement before building.

## Part 1 — the guard

### The ratchet

The original plan's instruction still holds and is the key design idea: *enable
one palette at a time as each bucket completes, so it ratchets rather than
gates.* A guard that fails on day one is a guard someone disables.

Current state (verified; re-verify before trusting — see the regex note below).
Counts are raw palette **utility** usages in `src/app`, excluding `*.spec.ts`:

| Palette | Hits | Status |
| --- | --- | --- |
| red, amber, green, emerald, yellow, rose, cyan, teal, lime, fuchsia, pink, violet | 0 | **ban immediately** |
| sky | 1 | 1 file |
| blue | 3 | 2 files |
| indigo | 5 | 5 files |
| purple | 9 | 6 files |
| orange | 24 | mostly one page |

So **12 of 17 palettes can be banned today.** Ship the guard with those 12
enabled, and leave the remaining 5 as explicitly-listed pending palettes with a
comment pointing at what has to happen first. Every time one clears, move it
into the banned list — that's the ratchet.

Do **not** ban neutrals. Per `tailwind-colors.md`, `gray`, `slate`, `zinc`,
`neutral`, `stone`, `white`, and `black` are outside the themed surface and are
explicitly allowed. A guard that flags `text-gray-500` would be wrong and
would generate thousands of false failures.

### Get the regex right (two traps)

This bit me while surveying, so save yourself the cycle:

- **A naive `"$color-\d+"` over-matches comments.** Most apparent "violations"
  left in the tree are documentation, e.g. `chart-colors.constants.ts` contains
  `'#3b82f6', // blue-500` — a comment naming the source step, which is
  desirable, not a violation. Ditto `/* slate-800 */` in a CSS comment and the
  `<!-- phase-3-outlier-colors: intentional border-t-orange-500 ... -->`
  annotation in `api-keys.page.ts`.
- **A lookbehind like `(?<![a-z-])$color-\d+` under-matches**, silently missing
  every real hit, because real usages are always prefixed (`bg-purple-100` has
  a `-` immediately before `purple`). I got fooled into thinking purple was at
  zero this way.

Anchor on the utility prefix instead. This pattern produced the counts above:

```
(?:bg|text|border|ring|outline|fill|stroke|from|via|to|shadow|divide|accent|caret|decoration|placeholder)-(?:blue|red|...)-\d+
```

Decide how to handle comments in the guard. Options, roughly in order of
preference: strip `//`, `/* */`, and `<!-- -->` comments before matching;
or match only inside `class="..."`/`[class...]`/`'...'` contexts; or keep a
small explicit allowlist of `file:line` exceptions. Whatever you choose, the
guard must not flag the legitimate documentation comments listed above.

### Shape of the guard

- Location: alongside the precedent, e.g. `src/app/color-tokens.spec.ts`
  (or `src/styles/color-hygiene.spec.ts` if that reads better).
- It needs filesystem access, so it likely wants `// @vitest-environment node`
  rather than the jsdom default. **Verify this actually works** under this
  project's `@analogjs/vitest-angular` builder before building the whole thing
  — write a trivial one-assertion version first and run it. If reading the
  source tree from a spec turns out to be awkward under that builder, fall
  back to a standalone node script wired into CI, and say so.
- Failure output must be actionable: list offending `file:line` plus the
  matched utility, and point at `tailwind-colors.md` for the mapping. A guard
  that just says "expected 3 to be 0" will get deleted by the next person who
  hits it.
- Header comment in the style of `global-hygiene.spec.ts`: what this enforces,
  why, what a failure means, and how to fix it (map to `primary-*` /
  `state-*` / `vendor-*` / `filetype-*`), including how to add a documented
  exception if one is genuinely warranted.

## Part 2 — raw hex mopup

Current state (verified, `src/**`, excluding `*.spec.ts` and generated files):
**177 hex values across 39 files** — 139 in `.ts`, 30 in `.css`, 8 in `.html`.

The original plan said "~206 raw hex values across 16 files." The hit count is
in the same ballpark; the file count is not, so re-scan rather than trusting
either number:

```powershell
Get-ChildItem -Recurse -Path src -Include *.ts,*.html,*.css | `
  Where-Object { $_.Name -notlike "*.spec.ts" -and $_.FullName -notlike "*generated*" } | `
  Select-String -Pattern "#[0-9a-fA-F]{3,8}\b"
```

Classify every hit. Known buckets:

- **Out of scope — generated.** `src/styles/generated/brand-theme.css` (~39
  hits) is machine-written from `brand.config.ts`. Never hand-edit; exclude
  from both the scan and any guard.
- **Out of scope — intentional JS interop.** `src/app/shared/constants/chart-colors.constants.ts`
  (~22 hits) was created in Phase 3 precisely because Chart.js needs resolved
  color strings, not utility classes. This is the one place raw hex is correct.
  If the guard also checks hex, this file must be allowlisted with a comment
  explaining why, so nobody "fixes" it later.
- **Out of scope — external data.** Values that are provider/tenant branding
  passed through from the backend (e.g. an OAuth provider's configured
  `button_color`, seen in `admin/auth-providers/pages/provider-form.page.ts`
  and `auth/login/login.page.ts`). This is data, not design-system color.
  Confirm each before excluding.
- **In scope — component CSS and inline styles.** The `.css` files
  (`tool-rail.component.css` ~16, `voice-overlay.component.css` ~9) and hex
  inside `.ts`/`.html` style bindings. For these, the fix is a CSS custom
  property referencing a token (`var(--color-state-danger-500)` etc.) rather
  than a utility class, since they're real CSS declarations. Check what the
  hex actually resolves to before swapping — if it's a neutral gray it may be
  fine as-is per the neutrals exemption.
- **In scope — needs a look.** `assistant-indicator.component.ts` (~27),
  `artifact-card.component.ts` (~23), `agent-icon.component.ts` (~12),
  `mcp-app-frame.component.ts` (~10). These are the biggest concentrations and
  are not yet characterized. Inspect them before deciding — some may be SVG
  fills, gradient stops, or canvas/drawing colors that need the same JS-interop
  treatment as the chart colors (i.e. a constants module) rather than tokens.

Handle this in chunks by file or directory, same incremental rhythm as earlier
phases. Where a hex maps cleanly onto an existing token, the swap should be
zero-pixel; where it doesn't, that's a recolor and needs to be called out.

## Coordination with the parallel Phase 3 track

`docs/prompts/phase-3-outlier-colors-and-icon-spinners.md` owns open decisions
that are **prerequisites for fully closing the ratchet**, specifically:

- The **`api-keys.page.ts` page-wide orange theme** (~24 of the 42 remaining
  hits, by far the largest block). That prompt asks whether the page's orange
  accent earns its own token or is a documented exception. Until that's
  answered, `orange` cannot be banned.
- The **indigo dialog boilerplate** — `focus:outline-indigo-600` on close
  buttons, confirmed across `confirmation-dialog.component.ts`,
  `tool-role-dialog.component.ts`, `delete-tool-dialog.component.ts`, and
  `manage-shares-dialog.component.ts`. The parallel prompt hypothesized this
  was a shared dialog convention rather than independent accidents; the spread
  across four dialogs supports that. Until it's resolved, `indigo` can't be
  banned.

Do not unilaterally resolve those two here — they're recolor/convention
decisions owned by that track. Phase 4's job is to make the guard tolerate them
today (pending palettes) and tighten once they land. The remaining stragglers
(purple category/decorative usages in `fine-tuning-costs`, `gemini-models`,
`role-form`; the deferred two-color gradients in `profile-settings` and
`agent-detail`; the lone `sky` hit) are smaller and may be fair game here —
check with the user whether to fold them into this phase or leave them to the
Phase 3 track.

## Verification

- `npm run build` after each chunk (`control_pwsh_process` background start +
  `get_process_output` polling; builds take ~40-90s and a direct
  `execute_pwsh` call will time out).
- Run the guard spec itself and confirm it **passes** on the current tree with
  the 12 clean palettes enabled. Then deliberately break it (temporarily add
  `bg-red-500` somewhere) and confirm it **fails with a useful message**. A
  guard that can't fail is worse than no guard. Revert the deliberate break.
- Run the full frontend suite (`npm run test:ci`) once at the end, since this
  phase adds a spec rather than only touching markup.
- Confirm the guard is actually reached by CI (it should be, via
  `.github/workflows/tests.yml` → `npm run test:ci`) — don't assume, check the
  workflow's working directory and command.
- Do not add visual regression snapshots — still out of scope.

## Before you start

Confirm with the user:
1. **Guard spec vs. ESLint** (or both) — this is the central decision of the
   phase and the point where the original plan needs amending. Do not start
   building either until this is settled.
2. Whether the purple/gradient/sky stragglers belong to this phase or the
   parallel Phase 3 track.
3. Whether the incorrect "ESLint (frontend)" line in
   `.kiro/steering/tech.md` should be corrected as part of this work.
