
You are an expert in TypeScript, Angular, and scalable web application development. You write functional, maintainable, performant, and accessible code following Angular and TypeScript best practices.

## TypeScript Best Practices

- Use strict type checking
- Prefer type inference when the type is obvious
- Avoid the `any` type; use `unknown` when type is uncertain

## Angular Best Practices

- Always use standalone components over NgModules
- Must NOT set `standalone: true` inside Angular decorators. It's the default in Angular v20+.
- Use signals for state management
- Implement lazy loading for feature routes
- Do NOT use the `@HostBinding` and `@HostListener` decorators. Put host bindings inside the `host` object of the `@Component` or `@Directive` decorator instead
- Use `NgOptimizedImage` for all static images.
  - `NgOptimizedImage` does not work for inline base64 images.

## Accessibility Requirements

- It MUST pass all AXE checks.
- It MUST follow all WCAG AA minimums, including focus management, color contrast, and ARIA attributes.

### Components

- Keep components small and focused on a single responsibility
- Use `input()` and `output()` functions instead of decorators
- Use `computed()` for derived state
- Set `changeDetection: ChangeDetectionStrategy.OnPush` in `@Component` decorator
- Prefer inline templates for small components
- Prefer Reactive forms instead of Template-driven ones
- Do NOT use `ngClass`, use `class` bindings instead
- Do NOT use `ngStyle`, use `style` bindings instead
- When using external templates/styles, use paths relative to the component TS file.

## State Management

- Use signals for local component state
- Use `computed()` for derived state
- Keep state transformations pure and predictable
- Do NOT use `mutate` on signals, use `update` or `set` instead

## Templates

- Keep templates simple and avoid complex logic
- Use native control flow (`@if`, `@for`, `@switch`) instead of `*ngIf`, `*ngFor`, `*ngSwitch`
- Use the async pipe to handle observables
- Do not assume globals like (`new Date()`) are available.
- Do not write arrow functions in templates (they are not supported).

## Services

- Design services around a single responsibility
- Use the `providedIn: 'root'` option for singleton services
- Use the `inject()` function instead of constructor injection

## Dialogs / Modals

- Use **`@angular/cdk/dialog`** for all modals. Do NOT use the native `<dialog>` element — it appears positioned correctly in isolation but breaks when an ancestor in the app shell creates a containing block (transforms, `will-change`, etc.), landing in the top-left of the viewport instead of centered.
- Each dialog is a **standalone component** in a `components/` subfolder of the feature that owns it (e.g. `admin/manage-models/components/add-curated-model-dialog.component.ts`).
- Export a `…DialogData` type for inputs and a `…DialogResult` type for the return value alongside the component. `undefined` from `dialogRef.closed` MUST mean "cancelled"; any concrete value means "confirmed."
- Inject `DialogRef<Result>` and `DIALOG_DATA` in the dialog component. Close via `this.dialogRef.close(value)`.
- Parent opens the dialog via `inject(Dialog).open<Result>(Component, { data })` and awaits the result with `firstValueFrom(dialogRef.closed)`. Bind `(keydown.escape)` in the dialog's `host` to call the cancel path so Escape, backdrop click, and the explicit Cancel button all converge.
- **Design tokens for dialogs match the host page's list-page idiom**, NOT the legacy form idiom: `rounded-2xl` (not `rounded-md` / `rounded-sm`), `text-sm/6` (not `text-sm`), `bg-blue-600` for the primary action (not indigo), with the panel built as `rounded-2xl border border-gray-200 bg-white … dark:border-gray-700 dark:bg-gray-800`. Older dialogs in the codebase (e.g. `admin/tools/components/tool-role-dialog.component.ts`) use the pre-redesign tokens — match their **structure** (backdrop div + centered panel + DialogRef wiring), not their **styling**.
- Canonical example: `admin/manage-models/components/add-curated-model-dialog.component.ts`.
