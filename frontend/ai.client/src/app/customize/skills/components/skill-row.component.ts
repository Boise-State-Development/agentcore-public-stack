import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroDocumentText,
  heroEllipsisVertical,
  heroPencilSquare,
  heroTrash,
} from '@ng-icons/heroicons/outline';

/**
 * One skill in the **Yours** list — the dense row idiom, not the browse card.
 *
 * Yours and Discover deliberately render the same data in two shapes. Discover
 * is a decision surface ("is this worth turning on?") and gets cards with room
 * for the description. Yours is a management surface over a list the user
 * already curated, so it optimises for scanning many rows and reaching the
 * actions — which is why the toggle is a labelled button here and a switch on
 * the card.
 *
 * Presentational only, like `CustomizeCardComponent`: it holds no service and
 * decides nothing. `owned` drives whether the destructive half of the menu
 * exists at all — editing and deleting are owner-only on the backend
 * (`UserSkillService` resolves ownership on every `/skills/mine/*` route), so
 * offering them on a catalog row would render an action that can only 404.
 */
@Component({
  selector: 'app-skill-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon, CdkMenu, CdkMenuItem, CdkMenuTrigger],
  host: { class: 'block' },
  providers: [
    provideIcons({ heroDocumentText, heroEllipsisVertical, heroPencilSquare, heroTrash }),
  ],
  template: `
    <div
      class="relative flex items-center gap-4 border-b border-gray-100 py-3.5 last:border-0 dark:border-white/5"
    >
      <span
        aria-hidden="true"
        class="grid size-9 shrink-0 place-items-center rounded-xl border border-gray-200 bg-gray-50 font-mono text-xs font-semibold text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300"
        >{{ monogram() }}</span
      >

      <div class="min-w-0 flex-1">
        <div class="flex min-w-0 items-center gap-2">
          <h3 class="min-w-0 truncate text-sm/6 font-semibold text-gray-900 dark:text-white">
            <a
              [routerLink]="detailLink()"
              class="after:absolute after:inset-0 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              >{{ name() }}</a
            >
          </h3>
          @if (badge(); as text) {
            <span
              class="shrink-0 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 font-medium text-gray-600 capitalize dark:bg-white/10 dark:text-gray-300"
              >{{ text }}</span
            >
          }
        </div>
        <p class="mt-0.5 truncate text-xs/5 text-gray-500 dark:text-gray-400">
          <span class="text-gray-400 dark:text-gray-500">{{ byline() }}</span>
          @if (description()) {
            · {{ description() }}
          }
        </p>
      </div>

      @if (fileCount() > 0) {
        <span
          class="hidden shrink-0 items-center gap-1 text-xs/5 text-gray-400 sm:flex dark:text-gray-500"
          [attr.aria-label]="fileCount() === 1 ? '1 file' : fileCount() + ' files'"
        >
          <ng-icon name="heroDocumentText" class="size-3.5" aria-hidden="true" />
          {{ fileCount() }}
        </span>
      }

      @if (meta(); as text) {
        <span class="hidden shrink-0 text-xs/5 text-gray-400 sm:block dark:text-gray-500">{{
          text
        }}</span>
      }

      <!-- Raised out of the row link's after:inset-0 overlay so both stay
           reachable — same control-in-control rule as CustomizeCardComponent. -->
      <div class="relative z-10 flex shrink-0 items-center gap-1">
        @if (toggleable() && !enabled()) {
          <button
            type="button"
            [disabled]="pending()"
            (click)="toggled.emit()"
            class="rounded-2xl border border-gray-200 bg-white px-3 py-1 text-sm/6 font-medium text-gray-700 transition-colors hover:border-gray-300 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:text-white"
          >
            {{ pending() ? 'Turning on…' : 'Turn on' }}
          </button>
        }

        <button
          type="button"
          [cdkMenuTriggerFor]="rowMenu"
          [attr.aria-label]="'Actions for ' + name()"
          class="grid size-8 place-items-center rounded-2xl text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
        >
          <ng-icon name="heroEllipsisVertical" class="size-5" aria-hidden="true" />
        </button>
      </div>

      <ng-template #rowMenu>
        <div
          cdkMenu
          role="menu"
          aria-orientation="vertical"
          class="w-48 rounded-xl bg-white py-1 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
        >
          <a
            cdkMenuItem
            role="menuitem"
            [routerLink]="detailLink()"
            class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
          >
            View details
          </a>

          @if (toggleable() && enabled()) {
            <button
              cdkMenuItem
              role="menuitem"
              type="button"
              [disabled]="pending()"
              (click)="toggled.emit()"
              class="flex w-full items-center gap-3 px-3 py-2 text-left text-sm/6 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            >
              Turn off
            </button>
          }

          @if (owned()) {
            <a
              cdkMenuItem
              role="menuitem"
              [routerLink]="editLink()"
              class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            >
              <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
              Edit
            </a>
            <button
              cdkMenuItem
              role="menuitem"
              type="button"
              (click)="deleted.emit()"
              class="flex w-full items-center gap-3 px-3 py-2 text-left text-sm/6 text-state-danger-700 outline-hidden hover:bg-state-danger-50 focus:bg-state-danger-50 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20 dark:focus:bg-state-danger-900/20"
            >
              <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
              Delete
            </button>
          }
        </div>
      </ng-template>
    </div>
  `,
})
export class SkillRowComponent {
  readonly name = input.required<string>();
  readonly description = input<string>('');
  readonly monogram = input<string>('?');
  /** "by you" / "from the catalog" — the origin line under the name. */
  readonly byline = input<string>('');
  /** Status or category chip beside the name, or null to draw none. */
  readonly badge = input<string | null>(null);
  /** Right-aligned secondary fact, typically the updated date. */
  readonly meta = input<string | null>(null);
  readonly fileCount = input<number>(0);
  readonly enabled = input<boolean>(false);
  /**
   * Whether this skill can be switched at all. False for a row the picker feed
   * does not carry — a DRAFT or DISABLED skill you authored is absent from
   * `GET /skills/` by design, so `toggleSkill` would find no row and return
   * silently. Rendering the button anyway would be a control that does nothing.
   */
  readonly toggleable = input<boolean>(true);
  /** In-flight toggle: the row stays put but refuses a second click. */
  readonly pending = input<boolean>(false);
  /** Owner-only actions (edit, delete) exist only when true. */
  readonly owned = input<boolean>(false);
  readonly detailLink = input.required<string>();
  readonly editLink = input<string>('');

  readonly toggled = output<void>();
  readonly deleted = output<void>();
}
