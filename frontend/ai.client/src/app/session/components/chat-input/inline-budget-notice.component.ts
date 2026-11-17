import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroInformationCircle } from '@ng-icons/heroicons/outline';
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
 * Under the composer's attachment cards: what the message's combined inline
 * budget does to its files, said before send.
 *
 * Two lines, in two tones, because they ask different things of the user:
 *
 * - A document past the budget is still attached and is read on demand, as a
 *   large document is. That needs nothing from the user, so it is a quiet info
 *   line, not a warning: an amber box here read as an upload error, and testers
 *   stopped instead of sending messages that would have worked. It is still
 *   named, though, because the model can answer without reading it; knowing
 *   which file that is lets the user ask about it by name.
 * - An image past the budget has no other way in, so it will not be sent at
 *   all. That stays a warning, since the fix is a follow-up message.
 *
 * Nothing renders while everything fits. The host is the live region and stays
 * in the DOM while the cards do, so a line is announced when it appears rather
 * than inserted with its own region.
 */
@Component({
  selector: 'app-inline-budget-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle, heroInformationCircle })],
  host: { role: 'status', 'aria-live': 'polite', class: 'block' },
  template: `
    @if (byReference().length) {
      <p
        class="mx-4 mb-3 flex items-start gap-2 text-xs/5 text-gray-600 dark:text-gray-400"
        data-testid="inline-budget-on-demand"
      >
        <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span class="min-w-0 flex-1">
          <span class="font-medium text-gray-700 dark:text-gray-300">{{ referenceNames() }}</span>
          will be read on demand rather than included in full,
          since this message's files together are over {{ budget() }}. Ask about
          {{ byReference().length === 1 ? 'it' : 'them' }} by name if the answer leaves
          {{ byReference().length === 1 ? 'it' : 'them' }} out.
        </span>
      </p>
    }
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
  protected readonly byReference = computed(() => this.overflow()?.byReference ?? []);
  protected readonly referenceNames = computed(() => joinNames(this.byReference()));
  protected readonly dropped = computed(() => this.overflow()?.dropped ?? []);
  protected readonly droppedNames = computed(() => joinNames(this.dropped()));
}
