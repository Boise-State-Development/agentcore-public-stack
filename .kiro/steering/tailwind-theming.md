---
inclusion: manual
---

# Theming Reference

> **Brand color scales are generated — never hand-write them.**
> `primary-*`, `secondary-*` and `tertiary-*` are emitted into
> `src/styles/generated/brand-theme.css` from `src/branding/brand.config.ts`.
> To change a brand color, edit that config. Status and category colors live
> in `src/styles/tokens/`. See the Colors reference for the full picture; the
> `@theme` examples below are for non-color theme values (spacing, radius,
> fonts) and for understanding what the generator emits.

## @theme Configuration

Define custom theme values in your CSS:

```css
@import 'tailwindcss';

@theme {
  /* Custom spacing */
  --spacing-18: 4.5rem;
  
  /* Custom radius */
  --radius-pill: 9999px;
  
  /* Custom font */
  --font-display: 'InterVariable', sans-serif;
}
```

## CSS Variables Access

Access theme values in custom CSS:

```css
.custom-element {
  background: var(--color-primary-500);
  border-radius: var(--radius-lg);
  padding: var(--spacing-4);
}

/* Spacing function for calculations */
.custom-layout {
  margin-top: calc(100vh - --spacing(16));
}
```

## Light/Dark Mode Setup

### Supporting Both Mechanisms

Support class-based toggle AND system preference:

```css
@import 'tailwindcss';

/* Dark mode activates with .dark class OR system preference */
@variant dark (&:where(.dark, .dark *));
@variant dark (@media (prefers-color-scheme: dark));
```

### Theme-Aware Colors

This project styles light/dark with explicit `dark:` variants on Tailwind
neutrals rather than flipping custom surface tokens:

```html
<div class="bg-white text-gray-900 dark:bg-gray-900 dark:text-white">
  Content adapts to mode
</div>
```

Brand colors are the exception worth knowing: a solid brand fill carrying white
text needs **no** `dark:` override, because the contrast that matters is fill
against its white text, which does not change between modes.

```html
<button class="bg-primary-accessible text-white hover:brightness-95">Save</button>
```

## Dark Mode Patterns

### Basic Pattern

Light styles first, then dark overrides:

```html
<div class="bg-white text-gray-900 dark:bg-gray-900 dark:text-white">
  Mode-aware content
</div>
```

### Ensuring Neither Mode Is Neglected

Always test both modes. Common oversights:

```html
<!-- ❌ Forgot dark mode -->
<div class="bg-white text-gray-900">
  Invisible in dark mode
</div>

<!-- ❌ Forgot light mode -->
<div class="dark:bg-gray-900 dark:text-white">
  Unstyled in light mode
</div>

<!-- ✅ Both modes covered -->
<div class="bg-white text-gray-900 dark:bg-gray-900 dark:text-white">
  Works in both modes
</div>
```

### Systematic Approach

1. Design light mode first with full styling
2. Add `dark:` variants for every color utility
3. Test by toggling modes frequently during development

### Border and Divide Colors

Don't forget borders:

```html
<div class="border border-gray-200 dark:border-gray-700">
  <div class="divide-y divide-gray-200 dark:divide-gray-700">
    <div>Item 1</div>
    <div>Item 2</div>
  </div>
</div>
```

### Ring and Focus Colors

```html
<button class="
  focus-visible:ring-2 
  focus-visible:ring-primary-accessible 
  focus-visible:ring-offset-2
  focus-visible:ring-offset-white
  dark:focus-visible:ring-offset-gray-900
">
  Button
</button>
```

### Shadows in Dark Mode

Shadows are less visible on dark backgrounds:

```html
<!-- ✅ Adjusted shadow for dark mode -->
<div class="shadow-lg dark:shadow-2xl dark:shadow-black/25">
  Card
</div>
```

### Form Inputs

```html
<input class="
  bg-white border-gray-300 text-gray-900 placeholder-gray-400
  dark:bg-gray-800 dark:border-gray-600 dark:text-white dark:placeholder-gray-500
  focus:ring-primary-accessible focus:border-primary-accessible
"/>
```

## Color Palette Strategy

### Using oklch for Scales

This is what the generator emits for each brand role, shown for reference —
do not hand-write it. Holding chroma and hue while varying lightness keeps a
scale perceptually consistent:

```css
/* generated output, illustrative */
--color-primary-500: #0033a0;
--color-primary-400: oklch(from #0033a0 calc(l + 0.1) c h);
--color-primary-600: oklch(from #0033a0 calc(l - 0.1) c h);
```

It also emits `--color-primary-accessible` and
`--color-primary-accessible-dark`, which shift lightness only as far as needed
to clear WCAG AA. Use those wherever legibility depends on the color, since a
fixed numbered step is not safe for an arbitrary configured brand color.

### Semantic Color Naming

This project already has a semantic layer — use it rather than inventing
parallel names. Three groups, each with its own source file:

| Group | Utilities | Source |
| --- | --- | --- |
| Brand (accent, interactive) | `primary-*`, `secondary-*`, `tertiary-*` | `styles/generated/brand-theme.css` |
| Status (error, warning, success, info) | `state-danger-*`, `state-warning-*`, `state-success-*`, `state-info-*` | `styles/tokens/state.css` |
| Category (vendor, file type) | `vendor-*`, `filetype-*` | `styles/tokens/identity.css` |

Do not add `--color-success`, `--color-error` or similar aliases; `state-*`
already covers that role. Surfaces use Tailwind's neutrals directly
(`bg-white dark:bg-gray-900`), which need no token layer.

## Theme Toggle Implementation

### Angular Example

```typescript
// theme.service.ts
@Injectable({ providedIn: 'root' })
export class ThemeService {
  private darkMode = signal(false);
  
  constructor() {
    // Check system preference on init
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const stored = localStorage.getItem('theme');
    this.darkMode.set(stored === 'dark' || (!stored && prefersDark));
    this.applyTheme();
  }
  
  toggle() {
    this.darkMode.update(v => !v);
    localStorage.setItem('theme', this.darkMode() ? 'dark' : 'light');
    this.applyTheme();
  }
  
  private applyTheme() {
    document.documentElement.classList.toggle('dark', this.darkMode());
  }
}
```

```html
<!-- Theme toggle button -->
<button (click)="themeService.toggle()" class="p-2 rounded-sm">
  <svg class="size-5 dark:hidden"><!-- sun icon --></svg>
  <svg class="size-5 hidden dark:block"><!-- moon icon --></svg>
  <span class="sr-only">Toggle theme</span>
</button>
```

## Checklist

- [ ] Both light and dark modes have complete styling
- [ ] Borders and dividers adapt to mode
- [ ] Focus rings have appropriate offset colors
- [ ] Form inputs are styled for both modes
- [ ] Shadows are visible in dark mode
- [ ] Text contrast meets WCAG AA in both modes
- [ ] Theme toggle persists preference
- [ ] System preference is respected as default
