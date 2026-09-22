import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

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
 * The whole card is NOT a button. Given a `detailLink` the card's *body* becomes
 * a link and the switch stays its sibling — never a nested control-in-control: a
 * switch inside the link is a control you cannot reach by keyboard without also
 * following the link. The link's `after:absolute inset-0` makes the whole card a
 * click target for the navigation while the switch, raised on its own stacking
 * context, keeps its own hit area. Without a `detailLink` the body renders as
 * plain text, so a surface with no detail page to drill into is unchanged.
 */
@Component({
  selector: 'app-customize-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: { class: 'block h-full' },
  template: `
    <div
      class="relative flex h-full items-start gap-3 rounded-2xl border border-gray-200 bg-white p-4 transition-colors hover:border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600"
    >
      <span
        aria-hidden="true"
        class="grid size-9 shrink-0 place-items-center rounded-xl border border-gray-200 bg-gray-50 font-mono text-xs font-semibold text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300"
        >{{ monogram() }}</span
      >

      <div class="min-w-0 flex-1">
        <div class="flex min-w-0 items-center gap-1.5">
          <h3 class="min-w-0 truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
            @if (detailLink(); as link) {
              <a
                [routerLink]="link"
                class="after:absolute after:inset-0 after:rounded-2xl hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                >{{ name() }}</a
              >
            } @else {
              {{ name() }}
            }
          </h3>
          @if (retiring()) {
            <span
              class="shrink-0 rounded-sm bg-state-warning-50 px-1.5 font-mono text-[10px]/5 font-medium text-state-warning-700 dark:bg-state-warning-900/30 dark:text-state-warning-300"
              >retiring</span
            >
          }
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
        @if (locked()) {
          <p class="mt-1 text-xs/5 font-medium text-gray-600 dark:text-gray-300">
            {{ lockedReason }}
          </p>
        }
        @if (retiring()) {
          <p class="mt-1 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-300">
            {{ enabled() ? retiringOnReason : retiringOffReason }}{{ retiringDetail() ? ' ' + retiringDetail() : '' }}
          </p>
        }
      </div>

      <button
        type="button"
        role="switch"
        [attr.aria-checked]="enabled()"
        [attr.aria-label]="
          locked()
            ? name() + ' is required by your organization and cannot be turned off'
            : retiringLocked()
              ? name() + ' is being retired and can no longer be turned on'
              : (enabled() ? 'Disable ' : 'Enable ') + name()
        "
        [attr.aria-disabled]="locked() || retiringLocked() ? 'true' : null"
        [attr.title]="locked() ? lockedReason : retiringLocked() ? retiringTooltip() : null"
        [disabled]="pending() || locked() || retiringLocked()"
        (click)="toggled.emit()"
        [class.opacity-50]="pending() && !locked()"
        class="relative z-10 mt-0.5 inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed"
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
  /**
   * The switch is on and the user cannot change it — an administrator pinned
   * this capability. Rendered as policy rather than as a disabled control:
   * full opacity (a greyed switch reads as "broken" or "loading"), a stated
   * reason on the card, and the reason in the accessible name, because a
   * `title` tooltip reaches neither touch users nor a screen reader browsing
   * statically.
   */
  readonly locked = input<boolean>(false);
  protected readonly lockedReason = 'Required by your organization';
  /**
   * Being retired: the opposite asymmetry to {@link locked}. Where a locked card
   * is on and cannot be turned off, a retiring one can be turned off and cannot
   * be turned back on — so the switch is disabled only while it is already off.
   * A retiring card that is ON stays a live, fully working control, because the
   * action we want from the user is exactly that one flip.
   *
   * Never a claim that the capability is broken: the backend still grants it and
   * it still works. See docs/specs/mcp-server-retirement.md §7.
   */
  readonly retiring = input<boolean>(false);
  /**
   * What to do instead, and when it stops working — already composed into one
   * sentence by `retirementDetail()` so this component holds no copy rules of
   * its own. Empty when the admin recorded neither, in which case the card says
   * only that the capability is going away, which is all we actually know.
   */
  readonly retiringDetail = input<string>('');
  protected readonly retiringOnReason = 'Being retired — turn it off when you can.';
  protected readonly retiringOffReason = 'Being retired and can no longer be turned on.';
  /** Retiring AND already off — the one state in which the switch refuses. */
  protected readonly retiringLocked = computed(() => this.retiring() && !this.enabled());
  /**
   * The hover text on a refusing switch. Carries the detail too, since "why
   * won't this turn on?" and "what do I use instead?" are the same question.
   * The card body states both regardless, so nothing here is title-only —
   * a `title` reaches neither touch users nor a statically-browsing reader.
   */
  protected readonly retiringTooltip = computed(() => {
    const detail = this.retiringDetail();
    return detail ? `${this.retiringOffReason} ${detail}` : this.retiringOffReason;
  });
  /** In-flight save: the switch stays visually settled but refuses a second click. */
  readonly pending = input<boolean>(false);
  readonly badge = input<CustomizeCardBadge>(null);
  /**
   * Where the card's name links to, or null to render it as plain text. Given a
   * link, the whole card becomes the navigation target except for the switch.
   */
  readonly detailLink = input<string | null>(null);

  readonly toggled = output<void>();
}
