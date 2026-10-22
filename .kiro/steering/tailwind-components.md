---
inclusion: manual
---

# Accessible Component Patterns

Accessible, theme-aware component patterns for Tailwind CSS v4.1.

## Buttons

> Color tokens only — never built-in palettes like `blue-600` or `red-500`.
> See the Colors reference for the three token groups and when to use
> `-accessible` rather than a numbered step.

### Primary Button

Solid fill with white text, so the fill uses `primary-accessible` (guaranteed
AA against white text) with no `dark:` override, and hover is a brightness
filter rather than a darker step.

```html
<button class="
  inline-flex items-center justify-center gap-2
  px-4 py-2 min-h-11
  bg-primary-accessible text-white font-medium
  rounded-sm shadow-xs
  transition-[filter] hover:brightness-95
  focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible
  disabled:bg-gray-300 disabled:text-gray-500 disabled:cursor-not-allowed
  dark:disabled:bg-gray-700 dark:disabled:text-gray-400
">
  Button text
</button>
```

### Secondary Button

```html
<button class="
  inline-flex items-center justify-center gap-2
  px-4 py-2 min-h-11
  bg-white text-gray-900 font-medium
  border border-gray-300
  rounded-sm shadow-xs
  hover:bg-gray-50
  focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible
  dark:bg-gray-800 dark:text-white dark:border-gray-600 dark:hover:bg-gray-700
">
  Button text
</button>
```

### Icon Button

```html
<button class="
  inline-flex items-center justify-center
  size-11 p-2
  text-gray-500 hover:text-gray-700
  rounded-sm
  hover:bg-gray-100
  focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible
  dark:text-gray-400 dark:hover:text-white dark:hover:bg-gray-800
">
  <svg class="size-5" aria-hidden="true">...</svg>
  <span class="sr-only">Close</span>
</button>
```

## Form Inputs

### Text Input

```html
<div>
  <label for="email" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
    Email
  </label>
  <input
    type="email"
    id="email"
    class="
      mt-2 block w-full
      px-3 py-2
      bg-white text-gray-900 placeholder-gray-400
      border border-gray-300 rounded-sm shadow-xs
      focus:ring-2 focus:ring-primary-accessible focus:border-primary-accessible
      dark:bg-gray-800 dark:text-white dark:placeholder-gray-500
      dark:border-gray-600 dark:focus:ring-primary-accessible-dark
    "
    placeholder="you@example.com"
  />
</div>
```

### Input with Error

```html
<div>
  <label for="email-error" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
    Email
  </label>
  <input
    type="email"
    id="email-error"
    aria-invalid="true"
    aria-describedby="email-error-message"
    class="
      mt-2 block w-full
      px-3 py-2
      bg-white text-gray-900
      border-2 border-state-danger-500 rounded-sm
      focus:ring-2 focus:ring-state-danger-500 focus:border-state-danger-500
      dark:bg-gray-800 dark:text-white
    "
  />
  <p id="email-error-message" class="mt-2 text-sm text-state-danger-600 dark:text-state-danger-400" role="alert">
    Please enter a valid email address.
  </p>
</div>
```

### Select

```html
<div>
  <label for="country" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
    Country
  </label>
  <select
    id="country"
    class="
      mt-2 block w-full
      px-3 py-2
      bg-white text-gray-900
      border border-gray-300 rounded-sm shadow-xs
      focus:ring-2 focus:ring-primary-accessible focus:border-primary-accessible
      dark:bg-gray-800 dark:text-white dark:border-gray-600
    "
  >
    <option>United States</option>
    <option>Canada</option>
  </select>
</div>
```

### Checkbox

```html
<div class="flex items-center gap-3">
  <input
    type="checkbox"
    id="remember"
    class="
      size-4
      rounded-xs border-gray-300
      text-primary-accessible
      focus:ring-2 focus:ring-primary-accessible focus:ring-offset-2
      dark:border-gray-600 dark:bg-gray-800
      dark:focus:ring-offset-gray-900
    "
  />
  <label for="remember" class="text-sm/6 text-gray-900 dark:text-white">
    Remember me
  </label>
</div>
```

## Cards

### Basic Card

```html
<article class="
  bg-white rounded-sm shadow-sm
  border border-gray-200
  overflow-hidden
  dark:bg-gray-800 dark:border-gray-700
">
  <div class="p-6">
    <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
      Card Title
    </h3>
    <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-300">
      Card description text goes here.
    </p>
  </div>
</article>
```

### Interactive Card

```html
<a 
  href="#"
  class="
    block
    bg-white rounded-sm shadow-sm
    border border-gray-200
    overflow-hidden
    hover:shadow-md hover:border-gray-300
    focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible
    transition-shadow
    dark:bg-gray-800 dark:border-gray-700
    dark:hover:border-gray-600
  "
>
  <div class="p-6">
    <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
      Clickable Card
    </h3>
    <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-300">
      The entire card is clickable.
    </p>
  </div>
</a>
```

## Navigation

### Primary Navigation

```html
<nav aria-label="Main navigation" class="bg-white border-b border-gray-200 dark:bg-gray-900 dark:border-gray-700">
  <div class="max-w-7xl mx-auto px-4">
    <div class="flex items-center justify-between h-16">
      <a href="/" class="text-xl font-bold text-gray-900 dark:text-white">
        Logo
      </a>
      <ul class="flex items-center gap-1">
        <li>
          <a 
            href="#" 
            class="
              px-4 py-2 rounded-sm
              text-sm/6 font-medium
              text-gray-900 hover:bg-gray-100
              focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible
              dark:text-white dark:hover:bg-gray-800
            "
            aria-current="page"
          >
            Home
          </a>
        </li>
      </ul>
    </div>
  </div>
</nav>
```

### Skip Link

Always include a skip link for keyboard users:

```html
<a 
  href="#main-content"
  class="
    sr-only focus:not-sr-only
    focus:absolute focus:top-4 focus:left-4 focus:z-50
    focus:px-4 focus:py-2
    focus:bg-primary-accessible focus:text-white
    focus:rounded-sm focus:shadow-lg
  "
>
  Skip to main content
</a>
```

## Alerts

### Info Alert

```html
<div 
  role="alert"
  class="
    flex gap-3 p-4
    bg-state-info-50 text-state-info-800
    border border-state-info-200 rounded-sm
    dark:bg-state-info-900/20 dark:text-state-info-300 dark:border-state-info-800
  "
>
  <svg class="size-5 shrink-0 mt-0.5" aria-hidden="true">...</svg>
  <div>
    <h4 class="font-medium">Information</h4>
    <p class="mt-1 text-sm/6">This is an informational message.</p>
  </div>
</div>
```

### Error Alert

```html
<div 
  role="alert"
  class="
    flex gap-3 p-4
    bg-state-danger-50 text-state-danger-800
    border border-state-danger-200 rounded-sm
    dark:bg-state-danger-900/20 dark:text-state-danger-300 dark:border-state-danger-800
  "
>
  <svg class="size-5 shrink-0 mt-0.5" aria-hidden="true">...</svg>
  <div>
    <h4 class="font-medium">Error</h4>
    <p class="mt-1 text-sm/6">Something went wrong. Please try again.</p>
  </div>
</div>
```

## Loading States

### Spinner

```html
<div role="status" class="flex items-center gap-2">
  <svg 
    class="size-5 animate-spin text-primary-accessible" 
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
  </svg>
  <span class="sr-only">Loading...</span>
</div>
```

### Skeleton

```html
<div class="animate-pulse">
  <div class="h-4 bg-gray-200 rounded-sm w-3/4 dark:bg-gray-700"></div>
  <div class="mt-2 h-4 bg-gray-200 rounded-sm w-1/2 dark:bg-gray-700"></div>
</div>
```

## Accessibility Checklist per Component

- [ ] Visible focus states with adequate contrast
- [ ] Proper ARIA attributes where needed
- [ ] Screen reader text for icon-only buttons
- [ ] Labels associated with form inputs
- [ ] Error states announced with `role="alert"`
- [ ] Light and dark mode both styled
- [ ] Minimum 44×44px touch targets
- [ ] Motion respects `prefers-reduced-motion`
