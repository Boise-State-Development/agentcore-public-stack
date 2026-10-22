---
inclusion: manual
---

# Accessibility Reference (WCAG 2.1 AA)

## Color Contrast

### Minimum Ratios (AA)

- **Normal text**: 4.5:1
- **Large text** (18px+ or 14px+ bold): 3:1
- **UI components & graphics**: 3:1

### Safe Color Combinations

For text on backgrounds, ensure sufficient contrast:

```html
<!-- ✅ High contrast patterns -->
<p class="bg-white text-gray-900">Dark on light</p>
<p class="bg-gray-900 text-white">Light on dark</p>
<p class="bg-primary-accessible text-white">White on brand (contrast guaranteed by the token)</p>

<!-- ⚠️ Test these combinations -->
<p class="bg-gray-100 text-gray-600">May fail contrast</p>
```

### Testing Contrast

Use browser DevTools or tools like:
- axe DevTools extension
- WAVE extension
- Chrome's built-in contrast checker (inspect element → color picker)

### Opacity and Contrast

Opacity reduces contrast — always verify:

```html
<!-- ⚠️ Opacity affects readability -->
<p class="text-gray-900/70">Verify this passes 4.5:1</p>
```

## Focus States

Every interactive element must have a visible focus indicator.

### Focus Ring Pattern

```html
<!-- ✅ Standard focus ring -->
<button class="focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible focus-visible:ring-offset-2">
  Click me
</button>

<!-- ✅ For dark backgrounds -->
<button class="focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-gray-900">
  Click me
</button>
```

### Focus-Visible vs Focus

Use `focus-visible:` for keyboard-only focus states (hides ring on mouse click):

```html
<!-- ✅ Keyboard-only focus ring -->
<a href="#" class="focus-visible:ring-2 focus-visible:ring-primary-accessible">Link</a>

<!-- Use focus: when you want ring on all focus (including mouse) -->
<input class="focus:ring-2 focus:ring-primary-accessible" />
```

### Focus Within

Style parent when child is focused:

```html
<div class="focus-within:ring-2 focus-within:ring-primary-accessible">
  <input type="text" class="border-0 focus:ring-0" />
</div>
```

### Custom Focus Styles

Ensure 3:1 contrast for focus indicators:

```html
<!-- ✅ High-visibility focus -->
<button class="
  focus-visible:outline-2 
  focus-visible:outline-offset-2 
  focus-visible:outline-primary-accessible
">
  Accessible button
</button>
```

## Screen Reader Support

### Visually Hidden Content

Use `sr-only` for screen-reader-only text:

```html
<!-- Icon button with accessible label -->
<button>
  <svg class="size-5" aria-hidden="true">...</svg>
  <span class="sr-only">Close menu</span>
</button>

<!-- Skip link -->
<a href="#main" class="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4">
  Skip to main content
</a>
```

### Hiding from Screen Readers

Use `aria-hidden="true"` for decorative elements:

```html
<!-- Decorative icon -->
<span aria-hidden="true">🎉</span>

<!-- Decorative SVG -->
<svg aria-hidden="true" class="size-5">...</svg>
```

### Live Regions

For dynamic content updates:

```html
<!-- Polite announcement (waits for pause) -->
<div aria-live="polite" class="sr-only">
  {{ statusMessage }}
</div>

<!-- Assertive announcement (interrupts) -->
<div aria-live="assertive" class="sr-only">
  {{ errorMessage }}
</div>
```

### Form Labels

Always associate labels with inputs:

```html
<!-- ✅ Explicit association -->
<label for="email" class="block text-sm/6 font-medium">Email</label>
<input id="email" type="email" class="..." />

<!-- ✅ Implicit association -->
<label class="block">
  <span class="text-sm/6 font-medium">Email</span>
  <input type="email" class="..." />
</label>

<!-- ✅ Hidden label for icon inputs -->
<label for="search" class="sr-only">Search</label>
<input id="search" type="search" placeholder="Search..." />
```

### Error States

Announce errors to screen readers:

```html
<input 
  id="email" 
  type="email" 
  aria-invalid="true"
  aria-describedby="email-error"
  class="border-state-danger-500 focus:ring-state-danger-500"
/>
<p id="email-error" class="text-sm text-state-danger-600 dark:text-state-danger-400" role="alert">
  Please enter a valid email address
</p>
```

## Motion and Animation

### Reduced Motion

Respect user preferences:

```html
<!-- ✅ Disable animations when preferred -->
<div class="transition-transform motion-reduce:transition-none motion-reduce:transform-none">
  Animated content
</div>

<!-- ✅ Alternative: reduce animation intensity -->
<div class="duration-300 motion-reduce:duration-0">
  Fade in
</div>
```

### Safe Animation Patterns

```html
<!-- ✅ Opacity is generally safe -->
<div class="transition-opacity">...</div>

<!-- ⚠️ Transform can cause vestibular issues -->
<div class="transition-transform motion-reduce:transition-none">...</div>
```

## Interactive Elements

### Minimum Target Size

Interactive elements should be at least 44×44px:

```html
<!-- ✅ Adequate touch target -->
<button class="min-h-11 min-w-11 p-3">
  <svg class="size-5">...</svg>
</button>

<!-- ✅ Adequate link target -->
<a href="#" class="inline-block py-3 px-4">Link</a>
```

### Disabled States

Communicate disabled state visually and semantically:

```html
<button 
  disabled
  class="bg-gray-300 text-gray-500 cursor-not-allowed"
  aria-disabled="true"
>
  Disabled
</button>
```

## Semantic Structure

### Heading Hierarchy

Use proper heading levels (don't skip):

```html
<h1>Page Title</h1>
  <h2>Section</h2>
    <h3>Subsection</h3>
  <h2>Another Section</h2>
```

### Landmarks

Use semantic HTML for landmarks:

```html
<header>...</header>
<nav aria-label="Main">...</nav>
<main id="main">...</main>
<aside>...</aside>
<footer>...</footer>
```

## Checklist

Before shipping, verify:

- [ ] Color contrast meets 4.5:1 for text, 3:1 for UI
- [ ] All interactive elements have visible focus states
- [ ] Icon buttons have accessible labels
- [ ] Form inputs have associated labels
- [ ] Error messages are announced to screen readers
- [ ] Animations respect `prefers-reduced-motion`
- [ ] Touch targets are at least 44×44px
- [ ] Heading hierarchy is logical
- [ ] Skip link is present for keyboard users
