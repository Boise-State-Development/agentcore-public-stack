# App Conventions — List & Form Pages

Project-specific design language for the AgentCore Public Stack frontend (`frontend/ai.client`).
Canonical examples: `/admin/manage-models` and `/admin/tools` (lists), `/admin/tools/new` (form).
When building or restyling a list or form page, match these tokens — do **not** copy the
older boxed-card style in `model-form.page.html`.

## Design tokens

| Element | Token |
|---------|-------|
| Border radius (inputs, buttons, list containers, chips, icon buttons) | `rounded-2xl` |
| Checkboxes | `rounded` |
| Body / control text | `text-sm/6` |
| Helper & meta text | `text-xs/5` |
| Page title (`h1`) | `text-2xl/8 font-bold` |
| Section heading (`h2`) | `text-base/7 font-semibold` |
| Accent color | `blue` (600/500) — never `indigo` |
| Focus ring (inputs) | `focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500` |
| Focus ring (buttons/links) | `focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500` |

Every token has a dark-mode pair (`dark:*`). Test both modes.

## Page shell

```html
<div class="min-h-dvh">
  <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
    <!-- list pages: max-w-5xl · form pages: max-w-3xl -->
  </div>
</div>
```

## Form pages

Flat `<section>` blocks separated by a top border — **no boxed section cards**.

```html
<form [formGroup]="form" class="space-y-8">
  <section class="space-y-4">
    <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Basic information</h2>
    <!-- fields -->
  </section>
  <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
    <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Next section</h2>
  </section>
</form>
```

Field:

```html
<label for="x" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
  Label <span class="text-red-600">*</span>
</label>
<input
  id="x"
  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
  [class.border-red-500]="ctrl.invalid && ctrl.touched"
/>
<p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Error message</p>
```

Select (`rounded-2xl` selects need a custom chevron — see "Selects" below):

```html
<div class="relative inline-flex">
  <select class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white">…</select>
  <ng-icon name="heroChevronDown" class="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500" aria-hidden="true" />
</div>
```

Buttons:

```html
<!-- Primary -->
<button class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600">Save</button>

<!-- Secondary (bordered) -->
<button class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700">Share</button>

<!-- Tertiary (ghost — Cancel) -->
<button class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white">Cancel</button>

<!-- Icon-only (size-8, e.g. delete) -->
<button class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400">…</button>
```

## Tabs (segmented underline)

Underline tabs inside a dialog or section — use `aria-selected` to drive the active
state so styling rides an attribute-selector variant. Do **not** use parallel
`[class.border-b-blue-600]` bindings (see "Common gotchas").

```html
<div class="flex gap-1 border-b border-gray-200 dark:border-gray-700" role="tablist">
  <button
    type="button"
    role="tab"
    [attr.aria-selected]="active()"
    (click)="active.set(true)"
    class="-mb-px inline-flex items-center gap-1.5 border-b-2 border-b-transparent px-3 py-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 aria-selected:border-b-blue-600 aria-selected:font-semibold aria-selected:text-blue-600 dark:text-gray-400 dark:hover:text-white dark:aria-selected:border-b-blue-400 dark:aria-selected:text-blue-400"
  >
    Tab label
  </button>
</div>
```

- `-mb-px` on each tab pulls its 2px bottom border down 1px so the active underline
  overlaps the container's 1px bottom border cleanly (no gray line peeking through).
- `border-b-*` (bottom-only) — not `border-*` — so the cascade fight is on
  `border-bottom-color` only.
- Active state flips text color + font weight in addition to the underline — short
  tab labels need the weight contrast to read at a glance.

## Common gotchas

### Conditional Tailwind classes can lose the cascade

Two classes that set the same property at the same specificity (`border-b-transparent`
base + `[class.border-b-blue-600]="active()"`) collide. Whichever Tailwind emits **later**
in the stylesheet wins, regardless of class order in your `class="…"` string. In practice
the transparent base wins and the active underline never appears.

Fix: drive the active state with an attribute selector that has higher specificity than
a plain class. Tailwind's built-in `aria-selected:`, `data-[…]:`, and `aria-*` variants all
generate selectors like `[aria-selected="true"]` (specificity `0,1,1`) which beat the
base utility (`0,1,0`):

```html
<button [attr.aria-selected]="active()"
        class="border-b-2 border-b-transparent aria-selected:border-b-blue-600">…</button>
```

DevTools symptom: the conditional class IS on the DOM, but `getComputedStyle(el).borderBottomColor` returns `rgba(0, 0, 0, 0)`. If you see that, this is the bug.

### Native `<select>` chevrons crowd `rounded-2xl` corners

Browsers position the native dropdown chevron at a fixed offset from the right edge,
ignoring `padding-right`. With `rounded-2xl` (1rem radius) the chevron overlaps the
curve. Adding more `pr-*` just pushes the text further left without moving the chevron.

Fix: `appearance-none` + an overlaid `heroChevronDown` icon (see the Select example
under "Form pages"). The wrapper handles positioning so the chevron clears the
rounded corner. `pointer-events-none` on the icon so clicks still fall through to the
native select.

## List pages

A list is a `<ul>` of `divide-y` rows inside a single `rounded-2xl` bordered container —
rows are **not** individually-bordered cards.

```html
<ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
  <li class="flex items-center gap-3 px-3 py-2.5 sm:px-4">…</li>
</ul>
```

Empty state:

```html
<div class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
  <p class="text-sm/6 text-gray-500 dark:text-gray-400">Nothing here yet.</p>
</div>
```

Chip / badge: `inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium` plus a
tinted `bg-*-100 text-*-800` pair (status: green/yellow/red/blue; role tags: purple).

Spinner: `animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400` (use `border-2` at `size-5` or smaller).
