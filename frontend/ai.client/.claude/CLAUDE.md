
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

## Feature Flags

- UI feature switches live in `features` in `src/environments/environment*.ts` (typed by `src/environments/feature-flags.ts`), one file per build: `environment.ts` (local), `environment.development.ts` (deployed dev, `dev-deploy` configuration), `environment.production.ts` (prod).
- Read them with `inject(FEATURES)` from `src/app/services/features.ts`. Don't import an environment file into a component. In specs, provide `FEATURES`.
- Gate UI synchronously on the flag (`@if (features.x)`, route `canMatch`). Never hide a nav item behind a network probe.
- In-development features are `false` until an environment chooses to turn them on. The backend `*_ENABLED` flag is the real gate; keep both in step (root `CLAUDE.MD`, "Feature Flags").

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
- Parent opens the dialog via `inject(Dialog).open<Result>(Component, { data })` and awaits the result with `firstValueFrom(dialogRef.closed)`.
- **Draw the dialog with `<app-dialog-shell>`** (`components/dialog/dialog-shell.component.ts`): backdrop, panel, header with title, description and close button, scrolling body, and `dialogFooter` / `dialogActions` / `dialogIcon` / `appDialogDescription` slots. Wire `(closed)` to the cancel path; the shell already emits it for Escape, the close button and a click outside the panel, so don't also bind `(keydown.escape)` on the host. Canonical examples: `components/confirmation-dialog/confirmation-dialog.component.ts` (confirmation) and `admin/marketplace/components/decline-submission-dialog.component.ts` (form).
- **The accessible name belongs on CDK's container, not on a panel you draw.** `.cdk-dialog-container` is itself the `role="dialog"` element and is unnamed unless something names it (axe `aria-dialog-name`, critical). The shell does this from its title. A dialog whose layout really can't use the shell puts `appDialogTitle` on its heading (and `appDialogDescription` on its description) and draws **no** `role="dialog"`, `aria-modal` or `aria-labelledby` of its own: a named dialog inside an unnamed one is the bug. A confirmation or destructive prompt is an `alertdialog`: `dialogRole="alertdialog"` on the shell or the title.
- **Focus on open.** CDK focuses the first tabbable element, which is the header's close button unless something is marked `cdkFocusInitial`: put it on the first field of a form, and on Cancel in a confirmation.
- **Spec it through a real `Dialog`**: `openInCdkDialog` + `expectNamedDialog` from `src/testing/cdk-dialog.ts`. A spec that renders the component directly with a stub `DialogRef` has no container, so it can't see a missing name.
- **Design tokens for dialogs match the host page's list-page idiom**, NOT the legacy form idiom: `rounded-2xl` (not `rounded-md` / `rounded-sm`), `text-sm/6` (not `text-sm`), `bg-primary-accessible` for the primary action (not `bg-blue-600`, not indigo).
