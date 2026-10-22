# Task: extract a shared spinner component and migrate every inline loading spinner to it

## Context

This is Phase 2 of a 4-phase Tailwind color-token migration. Phase 1 (blue accent
pass — interactive elements to `primary-*`, informational notices to
`state-info-*`) is complete across the entire `frontend/ai.client/src/app` tree.
Throughout Phase 1, every inline `animate-spin` loading spinner was deliberately
left untouched and deferred to this phase, so there is now a real backlog: **115
`animate-spin` occurrences across 72 files**, almost all of them the same ring
spinner hand-copied with small variations (size, border color, dark-mode
override).

Read first:
- `.kiro/steering/tailwind-colors.md` — the color token rules (authoritative).
- `.kiro/steering/tailwind-ui.md` — general Tailwind v4 conventions used in this app.
- `frontend/ai.client/src/branding/README.md` section 6 — contributor-facing color rules.
- `frontend/ai.client/src/styles/tokens/state.css` — status tokens.
- `frontend/ai.client/src/app/components/pulsating-loader.component.ts` — an
  existing example of a small standalone loading component in this codebase, for
  style/selector/file-layout conventions (not a spinner, but same category of
  component).

All paths below are relative to `frontend/ai.client/`.

## The one hard rule

Never mix a zero-pixel refactor with a visible recolor in the same chunk. Each
chunk must be reviewable as either "this must look identical" or "this should
look different, and here's how." State which it is when you hand it over.

This phase is mostly a zero-pixel refactor (same ring spinner, same sizes, same
colors, just deduplicated into one component) — but a handful of call sites
currently use raw `blue-*`/`red-*`/`purple-*`/`indigo-*`/`orange-*` border
colors that were never resolved to tokens because Phase 1 skipped all spinners
categorically. Migrating those is a visible recolor (if the raw color and its
target token don't resolve to the same pixels) and must be called out separately
from the mechanical extraction. Don't silently fold a recolor into what's
supposed to be a no-op refactor.

## Step 1 — inventory before building anything

Run a fresh scan; do not trust any counts from prior sessions:

```powershell
Get-ChildItem -Recurse -Path src/app -Include *.ts,*.html | `
  Where-Object { $_.Name -notlike "*.spec.ts" } | `
  Select-String -Pattern "animate-spin"
```

Classify every hit into one of these shapes:

1. **Ring spinner** — `<div>` or `<span>` with `animate-spin rounded-full border-*
   border-t-*` (by far the most common; this is what the shared component
   should replace).
2. **SVG spinner** — `<svg>` with a `<circle class="opacity-25">` +
   `<path class="opacity-75">` (the classic Tailwind docs spinner markup, used in
   a few button-loading states like `tool-approval-prompt`,
   `oauth-consent-prompt`, `citation-display`).
3. **Icon spinner** — `<ng-icon name="heroArrowPath" ... [class.animate-spin]="...">`
   used for refresh buttons. These rotate an icon in place rather than showing a
   ring, and arguably aren't the same UI affordance — decide whether they belong
   in scope (see Step 2).
4. **Non-spinner `animate-spin` usage** — skim for anything that isn't a loading
   indicator at all before assuming every hit is in scope.

Note the distinct border-color combinations in play so you know what the
component's color variants need to cover, e.g.:
- `border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400`
  (by far the most common — this is the "default/neutral" spinner used on
  full-page and section loading states)
- `border-white/30 border-t-white` (on solid-color buttons, e.g. red/primary
  submit buttons, where the spinner sits on a colored fill)
- `border-t-primary-accessible` / `border-t-primary-accessible-dark` (already
  migrated in a few places during Phase 1 admin work — treat these as the
  target shape for the "brand" variant)
- One-off outliers: `border-t-red-600`, `border-t-purple-600`, `border-t-indigo-500`,
  `border-t-indigo-600`, `border-t-orange-500` — check each of these
  individually, they may be intentional status-colored spinners (e.g. a
  danger-flavored retry spinner) rather than copy-paste drift.

Report the inventory (counts per shape, per color variant, list of outlier
files) before writing the component. Don't start building until you've shown
this breakdown — the actual variant API depends on what's really out there.

## Step 2 — design the component

Once you know the real shape of the data, propose (and get a nod on) a small
API. A reasonable starting point, subject to what Step 1 finds:

```html
<app-spinner size="sm | md | lg" variant="neutral | brand | on-solid" />
```

- `size` maps to the existing `size-*` values in use (likely 3–4 sizes cover
  everything: `sm`≈4, `md`≈8, `lg`≈12, plus whatever oddballs like `size-3.5`
  show up — round to the nearest standard size rather than preserving every
  exact value, and call out any place where that changes the visual size even
  slightly).
- `variant` maps to the border-color combinations found in Step 1. `neutral` is
  the gray/blue-accent default (recolor to `primary-accessible` /
  `primary-accessible-dark` — see Step 3), `on-solid` is the white-on-white/30
  version for buttons with a solid color fill, `brand` is the
  `primary-accessible` version already used in a few places.
- Decide whether the icon-rotation spinners (`ng-icon` refresh buttons) belong
  in this component or stay as-is — they're a different visual pattern (a
  rotating icon vs. a ring), not obviously the same component. Default to
  leaving them alone unless there's a clean way to unify them; ask if unsure.
- `role="status"` and an `aria-label` (or a slotted/default sr-only label)
  should be baked into the component so every call site gets an accessible
  loading announcement for free — check which existing call sites already pass
  `aria-label="Loading..."` / `role="status"` and which are missing it
  entirely (several are, e.g. plain `<div class="animate-spin ...">` with no
  ARIA at all). Bringing every site up to the same accessibility baseline is a
  positive side effect of this refactor, not scope creep.
- Standalone Angular component, `ChangeDetectionStrategy.OnPush`, selector
  `app-spinner`, colocated in `src/app/components/spinner/` (matching the
  existing flat/small-component convention in `src/app/components/`, e.g.
  `pulsating-loader.component.ts`).

## Step 3 — the color decision

The dominant existing pattern is `border-t-blue-600 dark:border-t-blue-400` on a
`border-gray-300 dark:border-gray-600` track. Per the color rules, raw
`blue-*` is banned. Decide once, for the whole component, whether the default
spinner accent is `primary-accessible`/`primary-accessible-dark` (brand — my
recommendation, since these are almost universally generic "loading" states
with no informational meaning) or `state-info-*` (status). Do not decide this
per call site — that defeats the point of extracting a shared component. A few
call sites already use `border-t-primary-accessible`, which supports the brand
reading. Flag this decision explicitly when you hand off the chunk, since it's
the one part of this phase that changes a pixel.

## Step 4 — migrate call sites

- Break the 72 files into logical chunks (by top-level `app/` directory mirrors
  the Phase 1 grouping: `session/`, `components/`, `settings/`, `admin/`,
  `agents/`, `fine-tuning/`, `assistants/`, `memory-spaces/`, `memory/`,
  `schedules/`, `knowledge-base/`, `my-skills/`, `auth/`, `files/`,
  `manage-sessions/`). Work incrementally and wait for an "okay" between chunks,
  same as Phase 1, unless told otherwise.
- For each call site: import `SpinnerComponent`, replace the inline markup with
  `<app-spinner size="..." variant="..." />`, remove now-unused imports if a
  file no longer needs `NgIcon`/whatever for anything else.
- Preserve `[class.animate-spin]`-style conditional spinners (Step 1, shape 3)
  as-is unless Step 2 decided to fold them in.
- Watch for spinners inside `@if (loading()) { ... }` blocks where the
  surrounding wrapper div (padding, centering, `role="status"` on the wrapper
  instead of the spinner) may become redundant once the component carries its
  own `role="status"` — don't leave duplicate ARIA roles on parent and child.

## Step 5 — outliers

Handle `border-t-red-600`, `border-t-purple-600`, `border-t-indigo-*`,
`border-t-orange-500` (and anything else Step 1 turns up) as individual
decisions, not folded into the default variant. Some may be intentional
(e.g. a spinner inside a danger-colored retry button should probably stay
red-ish, which would map to `state-danger-*` not the component's default
brand accent) — check the surrounding button/context for each before deciding.
If the component's `variant` prop doesn't cleanly cover one of these, it's fine
to leave that one call site as a documented exception rather than distorting
the component API for a single outlier.

## Verification

- Run `npm run build` after each chunk (use `control_pwsh_process` background
  start + `get_process_output` polling — builds take ~40-90s and direct
  `execute_pwsh` will time out).
- After all chunks: re-scan for `animate-spin` outside
  `components/spinner/spinner.component.ts` itself and confirm every remaining
  hit is a deliberately-excluded shape from Step 2 (icon-rotation spinners) or
  a documented Step 5 exception — not a miss.
- Check `.spec.ts` files for any test that asserts on the old inline spinner
  markup or class list (Phase 1 hit this in `quota-card.component.spec.ts` and
  `status-badge.component.spec.ts`) and update assertions to match the new
  component/classes.
- Do not add new unit tests or visual regression snapshots for the spinner
  component itself unless asked — out of scope, same as Phase 1.

## Before you start

Ask any clarifying questions after you've read the material above and done the
Step 1 inventory — in particular, confirm the `variant` naming/count and the
Step 3 color decision before touching any call site, since both are hard to
walk back once 70 files depend on them.
