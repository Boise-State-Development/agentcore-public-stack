import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

export type SpinnerSize = 'sm' | 'md' | 'lg' | 'xl';
export type SpinnerVariant = 'brand' | 'on-solid' | 'danger';

const SIZE_CLASSES: Record<SpinnerSize, string> = {
  sm: 'size-4 border-2',
  md: 'size-6 border-2',
  lg: 'size-8 border-4',
  xl: 'size-12 border-4',
};

const VARIANT_CLASSES: Record<SpinnerVariant, string> = {
  brand:
    'border-gray-300 border-t-primary-accessible dark:border-gray-600 dark:border-t-primary-accessible-dark',
  'on-solid': 'border-white/30 border-t-white',
  danger: 'border-state-danger-200 border-t-state-danger-600 dark:border-state-danger-800 dark:border-t-state-danger-400',
};

/**
 * SpinnerComponent
 *
 * Shared ring loading indicator. Replaces the hand-copied
 * `animate-spin rounded-full border-*` markup that was previously
 * duplicated across the app.
 *
 * - `size` maps to the app's existing `size-*` scale (sm=4, md=6, lg=8, xl=12).
 * - `variant` maps to the surface the spinner sits on:
 *   - `brand` (default): gray track + brand-accent arc, for plain page/section
 *     backgrounds.
 *   - `on-solid`: translucent white track + white arc, for buttons with a
 *     solid color fill.
 *   - `danger`: gray-red track + danger-accent arc, for destructive actions
 *     (delete/revoke) on a plain background.
 * - Carries `role="status"` and an accessible label by default so every call
 *   site gets a loading announcement for free. Pass `label` to customize it.
 *
 * **Icon-rotation spinners are intentionally separate:**
 * This component renders a ring spinner (generic "loading, please wait").
 * Icon-rotation spinners (`[class.animate-spin]` on `<ng-icon>`) are a different
 * visual pattern used for contextual actions (refresh, sync, discover, download)
 * where the icon itself implies the action. They use different styling (icon
 * colors, sizes) and mean different things. Do not merge them into this component
 * without explicit UX review.
 *
 * @example
 * ```html
 * <app-spinner size="lg" />
 * <app-spinner size="sm" variant="on-solid" label="Deleting" />
 * ```
 */
@Component({
  selector: 'app-spinner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="inline-block shrink-0 animate-spin rounded-full {{ sizeClass() }} {{ variantClass() }}"
      role="status"
      [attr.aria-label]="label()"
    ></div>
  `,
})
export class SpinnerComponent {
  readonly size = input<SpinnerSize>('md');
  readonly variant = input<SpinnerVariant>('brand');
  readonly label = input<string>('Loading');

  protected readonly sizeClass = computed(() => SIZE_CLASSES[this.size()]);
  protected readonly variantClass = computed(() => VARIANT_CLASSES[this.variant()]);
}
