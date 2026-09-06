# Task: resolve the Phase 2 outliers and decide the fate of icon-rotation spinners

## Context

Phase 2 (spinner component extraction) is complete: 99 of the 115 original
`animate-spin` ring/SVG spinners were replaced with `<app-spinner>` across
~65 files, recoloring the dominant `border-t-blue-600` pattern to
`primary-accessible`/`primary-accessible-dark` per the Phase 2 color decision.
That work is done and merged; do not redo it.

Phase 2 deliberately left two categories untouched, and this phase closes
both out:

1. **Four raw-color outliers** — spinners using `purple-600`, `indigo-600`,
   `indigo-500`, and `orange-500` instead of a token. Phase 2 flagged these
   individually rather than folding them into the new component's `brand`
   variant, per the rule that a recolor must never ride along inside a
   zero-pixel refactor.
2. **Icon-rotation spinners** — `<ng-icon name="heroArrowPath" ... [class.animate-spin]="...">`
   on refresh/sync/discover buttons. Phase 2 defaulted to leaving these alone
   since they're a different visual pattern (a rotating icon vs. a ring), but
   flagged the decision as open.

Read first:
- `.kiro/steering/tailwind-colors.md` — the color token rules (authoritative).
- `frontend/ai.client/src/app/components/spinner/spinner.component.ts` — the
  component built in Phase 2 (`size`: sm/md/lg/xl, `variant`: brand/on-solid/danger).
- `frontend/ai.client/src/branding/README.md` section 6 — contributor-facing
  color rules.

All paths below are relative to `frontend/ai.client/`.

## The one hard rule

Same as Phase 2: never mix a zero-pixel refactor with a visible recolor in
the same chunk. Every outlier decision below is a visible recolor (or an
explicit "stays weird" decision) — call each one out individually rather than
batching them under one heading.

## Part 1 — the four outliers

Re-verify these are still exactly where Phase 2 left them (a fresh scan, not
trusted from memory) before touching anything:

```powershell
Get-ChildItem -Recurse -Path src/app -Include *.ts,*.html | `
  Where-Object { $_.Name -notlike "*.spec.ts" } | `
  Select-String -Pattern "border-t-purple|border-t-indigo|border-t-orange"
```

For each hit, decide **stays colored** or **becomes `brand`** (i.e. gets
recolored to `primary-accessible` and switched to `<app-spinner variant="brand">`),
using the context Phase 2 already gathered:

1. **`admin/connectors/pages/connector-form.page.ts`** — `border-t-purple-600`
   on a "Loading roles..." spinner. Phase 2's read: likely copy-paste drift,
   no other purple styling on that section of the page (there's an unrelated
   purple checkbox accent nearby, but it's a different control). Recommend
   **fold into `brand`** unless you find something Phase 2 missed.

2. **`admin/tools/components/tool-role-dialog.component.ts`** — `border-t-indigo-600`
   inside a dialog whose checkboxes, selected-row highlight, and Save button
   are all indigo. Recommend **leave colored**, but not necessarily via the
   component's `variant` prop — check whether `state-info-*` or a bespoke
   indigo utility (if this app's dialog boilerplate is meant to be
   consistently indigo across several admin dialogs — see the next bullet)
   is the more honest fix than hardcoding `indigo-600` again.

3. **`manage-sessions/manage-shares-dialog/manage-shares-dialog.component.ts`** —
   `border-t-indigo-500` on a plain "Loading" spinner, in a dialog whose only
   other indigo is the close button's `focus:outline-indigo-600`. Phase 2
   noted this matches the same pattern as #2 and speculated it might be a
   **shared dialog template default** rather than two independent accidents.
   **Before deciding either of #2 or #3**, grep for `outline-indigo` and
   `border-t-indigo` across `src/app/**/*.ts` and `*.html` to see how many
   dialogs share this exact boilerplate (close button focus ring + loading
   spinner both indigo). If it's a real pattern across 3+ dialogs, that's a
   different fix than a one-off recolor: propose it as a shared dialog
   convention (e.g. document it in a steering file, or note that dialogs
   without explicit branding default to `state-info-*`) rather than silently
   converting some but not all instances to `brand`.

4. **`settings/pages/api-keys/api-keys.page.ts`** — `border-t-orange-500` on
   a "Loading models" spinner, inside a page section where the surrounding
   CTA buttons, icons, and borders are all orange (`bg-orange-500`,
   `text-orange-600`, `border-orange-300`, etc. — this looks like a
   page-local accent, not brand and not a state color). This is the clearest
   "leave it" case, but note that `orange-*` is currently a raw Tailwind
   palette color, which the color rules forbid in application code
   (`tailwind-colors.md`: "Never use Tailwind's built-in color palettes").
   Decide: does this whole page's orange theme deserve its own category
   token (e.g. a fourth brand role, or a category token if it's meant to
   distinguish "this is the API/developer section" the way `vendor-*` or
   `filetype-*` distinguish categories elsewhere), or is a hardcoded orange
   accent tolerated here as a pre-existing exception? Don't fix only the
   spinner's `orange-500` while leaving every other `orange-*` utility on the
   page untouched — that's a partial, inconsistent recolor. Either the whole
   page's orange theme gets a token, or document why it's exempt and leave
   all of it, spinner included.

For whichever outliers you decide **stay colored without a token**, add a
one-line comment above the markup (`<!-- intentional: matches page's ___ accent -->`)
so a future contributor doesn't "fix" it by accident. Phase 2 added exactly
this comment on the purple site as a placeholder; replace it with your actual
decision rather than leaving it as a TODO.

## Part 2 — icon-rotation spinners

Ten `[class.animate-spin]`/static-`animate-spin` `ng-icon` sites were left
alone in Phase 2. Full list (re-verify with the pattern below, not from this
list, in case something changed):

```powershell
Get-ChildItem -Recurse -Path src/app -Include *.ts,*.html | `
  Where-Object { $_.Name -notlike "*.spec.ts" } | `
  Select-String -Pattern "animate-spin" | `
  Select-String -Pattern "ng-icon|heroArrowPath"
```

Known sites (as of Phase 2 handoff): `admin/auth-providers/pages/provider-form.page.ts`
(discover button), `admin/auth-providers/pages/provider-list.page.ts` (test
connection), `admin/roles/pages/role-list.page.ts` (sync role), `components/file-card/file-card.component.ts`,
`components/model-settings/model-settings.html` (discover MCP servers),
`components/sidenav/components/session-list/session-list.html`, `files/file-browser.page.ts`
(refresh), `manage-sessions/manage-sessions.page.ts` (refresh), `session/components/export-dialog/export-dialog.component.ts`
(busy indicator), `session/components/message-list/components/artifact/artifact-panel.component.ts`
(downloading).

This is a genuine design decision, not a mechanical migration — resolve it
before writing any code:

- **Are these the same affordance as the ring spinner, just drawn
  differently?** A rotating `heroArrowPath` icon reads as "refresh/retry in
  progress" (the icon itself implies the action), whereas the ring spinner
  reads as generic "loading, please wait." They may be intentionally
  different vocabulary, not two implementations of one concept.
- If you conclude they're the same concept and should unify: `<app-spinner>`
  would need a new variant or prop that renders a rotating icon instead of a
  ring (a `shape="icon"` prop taking an icon name, or a completely separate
  `app-icon-spinner` component). Sketch the API and confirm before touching
  call sites, same as Phase 2's Step 2.
- If you conclude they should stay separate (this is the Phase 2 default and
  probably still correct): do nothing to these ten sites, but write down the
  reasoning somewhere durable (a code comment on `SpinnerComponent` itself,
  or a line in this phase's handoff) so Phase 4 or a future contributor
  doesn't reopen the question from scratch.

Don't let "should these unify" turn into scope creep on the four buttons that
use icon-rotation for something other than a refresh affordance (check each
call site's context, not just its markup, before grouping it with the
others).

## Verification

- Run `npm run build` after Part 1 and again after Part 2 (use
  `control_pwsh_process` background start + `get_process_output` polling,
  same as Phase 2 — builds take ~40-90s).
- Re-scan for `border-t-purple|border-t-indigo|border-t-orange` after Part 1
  and confirm every remaining hit has an explicit "intentional" comment, not
  a silent leftover.
- Re-scan for `animate-spin` after Part 2. If icon-rotation spinners were
  folded into a new shape, confirm the count of raw `ng-icon` + `animate-spin`
  combinations drops to zero (outside the four call sites you decided don't
  belong in the group, if any). If left alone, no count should change.
- Check `.spec.ts` files touched by either part for assertions on the old
  markup/classes (Phase 2 found none outstanding, but re-check anything you
  edit in this phase).
- Do not add new unit tests for the spinner component or any icon-spinner
  variant unless asked — out of scope, same as Phase 1 and Phase 2.

## Before you start

Confirm the Part 1 decisions (especially #2/#3's "shared dialog pattern"
question, since it could turn into a small refactor of its own rather than a
per-site fix) and the Part 2 direction (unify vs. leave separate) before
writing any code. Both are visible-recolor or API-shape decisions that are
harder to walk back once applied across multiple files, same caution as
Phase 2.
