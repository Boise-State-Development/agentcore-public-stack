import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

/** Connection chip shown beside a card's name, or null to draw none. */
export type CustomizeCardBadge = 'connected' | 'connect' | null;

/**
 * One capability card in the Customize grid — a tool or a skill.
 *
 * Presentational only: it holds no service and decides nothing. The pages own
 * "what is this" and "what happens when it flips"; this owns the shape. That
 * split is what lets Tools and Skills share one visual language while their
 * enable semantics stay opposite (tools default ON, skills default OFF —
 * Skills v2 D6).
 *
 * The whole card is NOT a button. The switch is the only control, so the card
 * carries no competing click target and the accessible name of the toggle is
 * the thing being toggled. Detail views arrive in a later step; when they do,
 * the body becomes a link and the switch stays a sibling, never a nested
 * button-in-button (see the drawer's own note at `model-settings.html:667`).
 */
@Component({
  selector: 'app-customize-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block h-full' },
  template: `
    <div
      class="flex h-full items-start gap-3 rounded-2xl border border-gray-200 bg-white p-4 transition-colors hover:border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600"
    >
      <span
        aria-hidden="true"
        class="grid size-9 shrink-0 place-items-center rounded-xl border border-gray-200 bg-gray-50 font-mono text-xs font-semibold text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300"
        >{{ monogram() }}</span
      >

      <div class="min-w-0 flex-1">
        <div class="flex min-w-0 items-center gap-1.5">
          <h3 class="min-w-0 truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
            {{ name() }}
          </h3>
          @switch (badge()) {
            @case ('connected') {
              <span
                class="shrink-0 rounded-sm bg-state-success-50 px-1.5 font-mono text-[10px]/5 font-medium text-state-success-700 dark:bg-state-success-900/30 dark:text-state-success-300"
                >connected</span
              >
            }
            @case ('connect') {
              <span
                class="shrink-0 rounded-sm bg-state-warning-50 px-1.5 font-mono text-[10px]/5 font-medium text-state-warning-700 dark:bg-state-warning-900/30 dark:text-state-warning-300"
                >connect</span
              >
            }
          }
        </div>
        <p class="mt-0.5 line-clamp-2 text-xs/5 text-gray-500 dark:text-gray-400">
          {{ description() }}
        </p>
      </div>

      <button
        type="button"
        role="switch"
        [attr.aria-checked]="enabled()"
        [attr.aria-label]="(enabled() ? 'Disable ' : 'Enable ') + name()"
        [disabled]="pending()"
        (click)="toggled.emit()"
        class="relative mt-0.5 inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        [class]="enabled() ? 'bg-primary-600 dark:bg-primary-500' : 'bg-gray-200 dark:bg-gray-700'"
      >
        <span
          aria-hidden="true"
          class="pointer-events-none inline-block size-5 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out"
          [class.translate-x-5]="enabled()"
          [class.translate-x-0]="!enabled()"
        ></span>
      </button>
    </div>
  `,
})
export class CustomizeCardComponent {
  readonly name = input.required<string>();
  readonly description = input<string>('');
  readonly monogram = input<string>('?');
  readonly enabled = input<boolean>(false);
  /** In-flight save: the switch stays visually settled but refuses a second click. */
  readonly pending = input<boolean>(false);
  readonly badge = input<CustomizeCardBadge>(null);

  readonly toggled = output<void>();
}
