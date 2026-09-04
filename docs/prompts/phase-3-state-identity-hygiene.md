# Task: state and identity color hygiene sweep

## Context

This is Phase 3 of a 4-phase Tailwind color-token migration. Phase 1 (blue
accent pass) and Phase 2 (shared spinner component) are complete. Unlike
Phase 1, this phase is almost entirely mechanical: red/amber/green/emerald map
to a fixed status meaning (danger/warning/success) essentially everywhere they
appear, so most of this phase is find-and-replace rather than judgment calls.
The exceptions are called out explicitly below — do not treat those as
precedent for bulk-replacing the rest.

Read first:
- `.kiro/steering/tailwind-colors.md` — the color token rules (authoritative).
- `frontend/ai.client/src/branding/README.md` section 6 — contributor-facing rules.
- `frontend/ai.client/src/styles/tokens/state.css` — status tokens
  (`state-danger-*`, `state-warning-*`, `state-success-*`, `state-info-*`).
- `frontend/ai.client/src/styles/tokens/identity.css` — vendor/file-type tokens,
  including the "categorical chart series colors deliberately do NOT live
  here" note at the bottom — that note is why this phase's chart work produces
  a TypeScript module instead of new CSS tokens.

All paths below are relative to `frontend/ai.client/`.

## The one hard rule

Never mix a zero-pixel refactor with a visible recolor in the same chunk. Each
chunk must be reviewable as either "this must look identical" or "this should
look different, and here's how." State which it is when you hand it over.

Nearly all of this phase should be zero-pixel: the state tokens were copied
verbatim (same OKLCH values) from the same Tailwind steps the raw utilities
currently use, so `bg-red-50` → `bg-state-danger-50` etc. should not move a
single pixel. The exceptions in Step 3 (green-that-means-file-type, not
green-that-means-success) are the only places a wrong call would actually
change what the color communicates, not just its class name.

## Step 1 — fresh inventory

Run this before touching anything; do not reuse counts from a prior session:

```powershell
$exts = @("*.ts","*.html")
$colors = @("red","amber","green","emerald","yellow","orange","purple","indigo","rose","sky","cyan","teal","lime","fuchsia","pink","violet")
foreach ($color in $colors) {
  $m = Get-ChildItem -Recurse -Path src/app -Include $exts | `
    Where-Object { $_.Name -notlike "*.spec.ts" } | `
    Select-String -Pattern "$color-\d+"
  "$color : $($m.Count) hits in $((($m | Group-Object Path).Count)) files"
}
```

Rough baseline from the last full-app scan (confirm, don't trust): `red` ~480
hits, `amber` ~128, `green` ~118, `emerald` ~28, `yellow` ~32, `purple` ~50,
`rose` ~48, `orange` ~41, `indigo` ~40. Also scan for raw hex:

```powershell
Get-ChildItem -Recurse -Path src/app -Include *.ts,*.html | `
  Where-Object { $_.Name -notlike "*.spec.ts" } | `
  Select-String -Pattern "#[0-9a-fA-F]{3,8}\b"
```

This turns up ~30 hits across ~28 files — most are Chart.js config objects
(`borderColor: '#3b82f6'`, tooltip `backgroundColor: isDarkMode ? '#1f2937' :
'#ffffff'`, etc. — TypeScript object literals, not Tailwind classes, so they
need a different fix than a class rename) plus a handful of genuinely unrelated
hex (e.g. an OAuth provider's brand `button_color` passed through from the
backend, which is correctly *not* in scope — that's per-provider branding data,
not app UI).

## Step 2 — the mechanical majority

For each of these, the mapping is fixed and should not require a judgment call
per site — apply it directly:

| Raw utility | Token | Notes |
| --- | --- | --- |
| `red-*` | `state-danger-*` | error/destructive text, banners, delete buttons, invalid-input rings |
| `amber-*`, `yellow-*` | `state-warning-*` | caution banners, "almost at limit" text, pending/awaiting-auth dots |
| `green-*` | `state-success-*` | success banners, "complete" status, checkmarks — **check each site against Step 3 first** |
| `emerald-*` | `state-success-*` | same status meaning as `green` in this codebase; confirm no site uses emerald for something else before treating it as an alias |

Work through files by top-level `app/` directory (mirrors the Phase 1/2
grouping: `session/`, `components/`, `settings/`, `admin/`, `agents/`,
`fine-tuning/`, `assistants/`, `memory-spaces/`, `memory/`, `schedules/`,
`knowledge-base/`, `my-skills/`, `auth/`, `files/`, `manage-sessions/`). Work
incrementally, wait for an "okay" between chunks unless told otherwise.

Straight class-string replacement is fine here (regex via `execute_pwsh` +
`Get-Content -Raw`/`Set-Content -NoNewline`, or `str_replace` for one-off
matches) — but per the lesson from Phase 1, **run `git diff --stat` after every
regex pass** to confirm only the intended files changed, especially with any
generic whitespace-cleanup regex.

## Step 3 — the exceptions (do not bulk-replace these)

A handful of `green`/other call sites mean something other than "success" and
must NOT become `state-success-*`:

- **Spreadsheet file badges** (csv/xls/xlsx) — `green` here means "this is a
  spreadsheet," a fixed file-type identity, not "this succeeded." These belong
  on `filetype-sheet-*` (already defined in `identity.css`), not
  `state-success-*`. Check `components/file-card/file-card.component.ts`,
  `session/.../file-attachment-badge`, and
  `session/.../renderers/file-download-renderer` specifically — these three
  were named in the original Phase 1 plan as having partially-started
  file-type maps; finish them here. Verify each file-type entry (PDF→rose/
  filetype-pdf, DOC/DOCX→filetype-doc, sheets→filetype-sheet, MD→
  filetype-markdown, images→filetype-image, HTML/code→filetype-code,
  presentations→filetype-presentation) resolves to the matching `identity.css`
  token rather than assuming only the green ones need attention.
- **Vendor/provider colors** — if any `green`/`purple`/`orange`/etc. hit turns
  out to be a specific connector or vendor's brand color (matching the
  Google/Microsoft/Canvas/Zoom pattern from Phase 1) rather than a generic
  decorative pick, it belongs in `identity.css` as a `vendor-*` token, not
  `state-*`. Check before assuming every non-red/amber color is fair game for
  `state-success`.
- **Per-item decorative palettes** — metric-card icon colors or category badge
  palettes where a color is one of several arbitrary hues distinguishing
  siblings with no status meaning (the pattern handled in Phase 1's Agent
  Marketplace chunk via `vendor-*` reuse). If you hit one of these, reuse an
  existing non-brand, non-state token rather than assuming it's a success
  indicator just because it happens to be green.
- When genuinely unsure whether a color is status or identity, ask — don't
  guess and bulk-apply.

## Step 4 — indigo, purple, rose, orange, sky, cyan, etc.

These don't have a single fixed meaning in this app the way red/amber/green
do. For each hit, check the surrounding context and classify it as one of:

1. **Actually a vendor/file-type identity color** (e.g. `indigo` used for
   image file badges, `rose` for PDF, per the existing `identity.css` map) —
   apply the matching `filetype-*`/`vendor-*` token.
2. **A decorative/arbitrary distinguishing color** with no fixed meaning (e.g.
   a checkbox accent, a per-item palette slot) — per the Phase 1 precedent,
   reuse an existing non-brand token that stays visually distinct from
   siblings, rather than inventing a new one.
3. **Mislabeled state color** — occasionally `purple` or another odd color
   shows up standing in for warning/info/success where whoever wrote the
   component just grabbed a color that looked fine at the time with no
   identity meaning at all. If it's genuinely a stray status color, migrate it
   to the correct `state-*` token instead of preserving the arbitrary choice.
4. **Genuinely out of scope** — a one-off decorative accent with no
   status/identity meaning and no sibling colors to stay distinct from (rare,
   but don't force a token if there's truly no rule to apply). Flag these
   rather than silently skipping them, so the user can weigh in.

This step will need more back-and-forth than Step 2. Batch similar findings
together and present them rather than asking one-by-one.

## Step 5 — chart series colors → TypeScript constants module

`admin/costs/components/model-breakdown.component.ts` has a hardcoded
`colors` array of 10 raw hex strings (`#3b82f6`, `#10b981`, `#f59e0b`,
`#ef4444`, `#8b5cf6`, `#ec4899`, `#06b6d4`, `#84cc16`, `#f97316`, `#6366f1`)
used to color pie/bar chart segments by index.
`admin/costs/components/cost-trends-chart.component.ts` has two more
(`#3b82f6` for the cost line, `#10b981` for the requests line, plus matching
`rgba(...)` fill colors and dark-mode-aware tooltip/grid/text colors).

Per `identity.css`'s note, these do not become CSS tokens — Chart.js needs a
resolved color string at render time, not a utility class. Create
`src/app/shared/constants/chart-colors.constants.ts` (matches the existing
convention in `shared/constants/session.constants.ts`) exporting:

- A named palette array for categorical/indexed series (replaces the
  `model-breakdown` color array) — keep the same 10 hex values unless there's
  a reason to change them, since this is meant to be zero-pixel.
  Well-known distinguishable colors, ideally the resolved sRGB values of the
  same Tailwind steps already in use (converting OKLCH tokens to a hex string
  for JS consumption is fine here — this is the one place raw hex is
  intentional, since it's JS interop, not a Tailwind utility).
- Named exports for the specific semantic series colors currently hardcoded in
  `cost-trends-chart` (`cost` line color, `requests` line color, and their
  fill/alpha variants).
- Optionally, a small helper for the dark-mode-aware chrome colors (tooltip
  bg/text, grid lines) that are currently duplicated across both chart
  components with slightly different variable names — check if unifying them
  is in scope or better left alone (they're not colors from the design system,
  just gray-scale chart chrome, so this may be lower priority than the actual
  data-series colors).

Update both components to import from the new module instead of hardcoding
literals. This module is JS/TS, so the "copy OKLCH literals verbatim" rule
doesn't apply the same way — resolved hex/rgba strings are correct here since
Chart.js can't consume CSS custom properties directly at canvas-render time.

## Step 6 — raw hex cleanup (the rest)

For hex values found in Step 1 that are NOT chart-related (i.e. not covered by
Step 5), classify each:
- If it's a Tailwind-palette color spelled out as hex instead of a utility
  class (e.g. someone wrote `style="color: #ef4444"` instead of
  `class="text-red-500"`) — convert to the matching utility class first, then
  apply the same token mapping as Step 2/3/4.
- If it's legitimately external data (an OAuth provider's configured
  `button_color`, a user-uploaded brand asset, etc.) — leave it. This is data,
  not a design-system color, and out of scope.
- If it's part of `dark:` conditional chart chrome already covered in Step 5,
  skip (handled there).

## Verification

- Run `npm run build` after each chunk (`control_pwsh_process` background
  start + `get_process_output` polling; builds take ~40-90s).
- After Step 5, manually sanity-check that `admin/costs` charts still render
  with the same visual colors (open the cost dashboard, or at minimum confirm
  the constants module exports the identical hex values that were removed
  from the components).
- Check `.spec.ts` files for assertions on any class string or color literal
  touched in this phase (Phase 1 hit this twice: `quota-card.component.spec.ts`,
  `status-badge.component.spec.ts`) and update them.
- Full re-scan at the end: rerun the Step 1 script and confirm the only
  remaining hits are the documented Step 3/4 exceptions and Step 6 external
  data, not misses.
- Do not add new unit tests or visual regression snapshots — out of scope,
  same as Phases 1 and 2.

## Before you start

Confirm the Step 1 counts and share the breakdown before starting Step 2 —
some of these numbers (480 `red` hits especially) suggest there may be
sub-chunking needed within a single color, not just within a directory tree.
Propose a chunking plan (by directory, as in Phase 1/2, or by color, or both)
before starting.
