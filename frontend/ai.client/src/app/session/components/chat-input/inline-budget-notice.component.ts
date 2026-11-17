import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import {
  AttachmentLimitsService,
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
 * Under the composer's attachment cards: the files the message's combined
 * inline budget will leave out, said before send.
 *
 * Only files that will not travel at all are named, which is an image past the
 * budget: it has no other way in, so the fix is a follow-up message. A document
 * past the budget is not mentioned. It is attached and read from storage in the
 * parts the answer needs, exactly as a large document is, and a warning about
 * it read as an upload error (testers stopped and reported "File Size" errors
 * for messages that would have worked). Nothing renders otherwise.
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
    @if (dropped().length) {
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
          <span class="font-medium">{{ droppedNames() }}</span>
          won't fit in this message, which can include {{ budget() }} of images and documents in full.
          Send {{ dropped().length === 1 ? 'it' : 'them' }} in a follow-up message.
        </p>
      </div>
    }
  `,
})
export class InlineBudgetNoticeComponent {
  readonly overflow = input<InlineBudgetOverflow | null>(null);

  private readonly limits = inject(AttachmentLimitsService).limits;

  /** The served budget (`GET /files/limits`). An overflow implies there is one. */
  protected readonly budget = computed(() => budgetLabel(this.limits().inlineBudgetBytes ?? 0));
  protected readonly dropped = computed(() => this.overflow()?.dropped ?? []);
  protected readonly droppedNames = computed(() => joinNames(this.dropped()));
}
