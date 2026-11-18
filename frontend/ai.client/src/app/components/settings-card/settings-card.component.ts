import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroBookOpen,
  heroChatBubbleLeftRight,
  heroChevronDown,
  heroCpuChip,
  heroMicrophone,
  heroSparkles,
  heroUserCircle,
  heroWrenchScrewdriver,
} from '@ng-icons/heroicons/outline';

let nextInstance = 0;

/**
 * One card on a settings form: a header (icon, title, a one-line summary of what
 * is set) over a body that can fold away. Every section of the agent composer is
 * one of these, so the page reads as a stack of answers ("Claude Sonnet 5.5",
 * "3 tools: Canvas, Drive, Web search") and each one opens to the controls behind
 * it only when the author wants them.
 *
 * The summary is the point of folding. A collapsed card with no summary trades a
 * click for nothing, so a collapsible card should always be given one.
 *
 * The body is projected, so whatever is in it is created once by the parent and
 * keeps its state while the card is folded; folding only takes it out of the page.
 */
@Component({
  selector: 'app-settings-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroBookOpen,
      heroChatBubbleLeftRight,
      heroChevronDown,
      heroCpuChip,
      heroMicrophone,
      heroSparkles,
      heroUserCircle,
      heroWrenchScrewdriver,
    }),
  ],
  host: { class: 'block' },
  template: `
    <section
      [attr.aria-labelledby]="headingId"
      class="rounded-2xl border border-gray-200/80 bg-white shadow-xs dark:border-gray-700/60 dark:bg-gray-800/80"
    >
      <div class="flex items-start gap-3 px-5 py-4 sm:px-6">
        <span class="mt-px flex size-8 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-500 dark:bg-white/5 dark:text-gray-400">
          <ng-icon [name]="icon()" class="size-4" aria-hidden="true" />
        </span>
        <div class="min-w-0 flex-1">
          <!-- The heading holds the toggle, not the other way round: a heading
               inside a button is invalid, and the summary stays out of the
               heading's name so a screen reader's headings list reads "Model",
               not a sentence. The summary describes the button instead. -->
          <h2 [id]="headingId" class="text-sm/6 font-semibold text-gray-900 dark:text-white">
            @if (collapsible()) {
              <button
                type="button"
                (click)="toggle()"
                [attr.aria-expanded]="open()"
                [attr.aria-controls]="bodyId"
                [attr.aria-describedby]="shownSummary() ? summaryId : null"
                class="flex w-full items-center gap-2 rounded-lg text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              >
                <span>{{ title() }}</span>
                @if (badge()) {
                  <span class="text-xs/5 font-medium text-gray-500 dark:text-gray-400">{{ badge() }}</span>
                }
                @if (count() !== null) {
                  <span class="rounded-full bg-gray-100 px-2 text-xs/5 font-medium tabular-nums text-gray-700 dark:bg-white/10 dark:text-gray-300">{{ count() }}</span>
                }
                <ng-icon
                  name="heroChevronDown"
                  class="ml-auto size-4 shrink-0 text-gray-400 transition-transform dark:text-gray-500"
                  [class.rotate-180]="open()"
                  aria-hidden="true"
                />
              </button>
            } @else {
              <span class="flex items-center gap-2">
                <span>{{ title() }}</span>
                @if (badge()) {
                  <span class="text-xs/5 font-medium text-gray-500 dark:text-gray-400">{{ badge() }}</span>
                }
              </span>
            }
          </h2>
          @if (shownSummary()) {
            <p [id]="summaryId" class="mt-0.5 pr-6 text-xs/5 text-gray-600 dark:text-gray-400" [class.truncate]="collapsible() && !open()">{{ shownSummary() }}</p>
          }
        </div>
        <ng-content select="[cardHeaderAction]" />
      </div>

      <!-- Outside the fold on purpose: something the author must act on (a retiring
           tool, a retired model) stays in view however the card is set. -->
      <ng-content select="[cardNotice]" />

      @if (open() || !collapsible()) {
        <div [id]="bodyId" class="border-t border-gray-200/70 px-5 py-5 sm:px-6 dark:border-gray-700/60">
          <ng-content />
        </div>
      }
    </section>
  `,
})
export class SettingsCardComponent {
  readonly title = input.required<string>();
  /** A heroicons name; the card provides the set the composer uses. */
  readonly icon = input('heroCpuChip');
  /** What is set, in one line. Shown under the title, truncated while folded. */
  readonly summary = input('');
  /** Shown instead of {@link summary} while open, when the open card wants a description rather than a read-out. */
  readonly openSummary = input('');
  /** A short word beside the title, such as "Required" or "Optional". Always text. */
  readonly badge = input('');
  /** A count beside the title, such as the number of tools selected. `null` hides it. */
  readonly count = input<number | null>(null);
  readonly collapsible = input(true);
  /** Two-way: `[(open)]`. */
  readonly open = model(true);

  private readonly uid = `settings-card-${nextInstance++}`;
  protected readonly headingId = `${this.uid}-title`;
  protected readonly bodyId = `${this.uid}-body`;
  protected readonly summaryId = `${this.uid}-summary`;

  protected readonly shownSummary = computed(() =>
    this.open() && this.openSummary() ? this.openSummary() : this.summary(),
  );

  protected toggle(): void {
    this.open.update((o) => !o);
  }
}
