import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import {
  INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
  type BudgetedAttachment,
  type InlineBudgetOverflow,
} from '../../../services/file-upload';

/** `a`, `a and b`, `a, b and c`. */
function joinNames(files: readonly BudgetedAttachment[]): string {
  const names = files.map(file => file.filename);
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** The budget in decimal megabytes, as the backend states it: `7 MB`. */
function budgetLabel(bytes: number): string {
  const mb = Math.round(bytes / 100_000) / 10;
  return `${mb.toString()} MB`;
}

/**
 * Under the composer's attachment cards: which files the message's combined
 * inline budget will leave out, said before send.
 *
 * Each file can pass the per-file limit and the message still exceed what one
 * message can carry in full, and until this the only sign was a footnote on the
 * answer. A document that does not fit is read from storage instead, in the
 * parts the answer needs; an image is not included, so the fix is a follow-up
 * message. Nothing renders while everything fits, which is the normal case.
 *
 * The host is the live region and stays in the DOM while the cards do, so the
 * notice is announced when it appears rather than inserted with its own region.
 */
@Component({
  selector: 'app-inline-budget-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { role: 'status', 'aria-live': 'polite', class: 'block' },
  template: `
    @if (overflow(); as over) {
      <div
        class="mx-4 mb-3 flex items-start gap-2 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-3 py-2 text-xs/5 text-state-warning-800 dark:border-state-warning-900/50 dark:bg-state-warning-900/30 dark:text-state-warning-200"
        data-testid="inline-budget-notice"
      >
        <ng-icon
          name="heroExclamationTriangle"
          class="mt-0.5 size-4 shrink-0 text-state-warning-700 dark:text-state-warning-400"
          aria-hidden="true"
        />
        <p class="min-w-0 flex-1">
          These attachments are over the {{ budget }} one message can include in full.
          @if (over.byReference.length) {
            <span class="font-medium">{{ referenceNames() }}</span>
            will be read from storage instead, in the parts the answer needs.
          }
          @if (over.dropped.length) {
            <span class="font-medium">{{ droppedNames() }}</span>
            {{ over.dropped.length === 1 ? "won't be included. Send it" : "won't be included. Send them" }}
            in a follow-up message.
          }
        </p>
      </div>
    }
  `,
})
export class InlineBudgetNoticeComponent {
  readonly overflow = input<InlineBudgetOverflow | null>(null);

  protected readonly budget = budgetLabel(INLINE_ATTACHMENTS_MAX_TOTAL_BYTES);
  protected readonly referenceNames = computed(() => joinNames(this.overflow()?.byReference ?? []));
  protected readonly droppedNames = computed(() => joinNames(this.overflow()?.dropped ?? []));
}
