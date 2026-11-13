import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight, heroXMark } from '@ng-icons/heroicons/outline';

import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { DeliveryStateService } from '../../services/handoff/delivery-state.service';
import { DeliveryService } from '../../services/handoff/delivery.service';

/**
 * Send to *{title}*'s review (handoffs spec §4.3), in the conversation a handoff started.
 *
 * The same idea and look as the handoff review sheet: a labelled region above the composer, not a
 * modal, so the answer being sent stays readable. The result is editable and each artifact the
 * answer made can be left behind. Send makes a card on the original conversation's composer and
 * the user stays here. Esc cancels.
 */
@Component({
  selector: 'app-send-back-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroArrowRight, heroXMark })],
  host: { '(keydown.escape)': 'cancel()' },
  template: `
    @if (review(); as r) {
      <section
        #sheet
        role="region"
        [attr.aria-label]="'Send to ' + sourceTitle()"
        tabindex="-1"
        class="mb-2 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
      >
        <div class="flex items-start justify-between gap-3">
          <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Send to {{ sourceTitle() }}</h2>
          <button
            type="button"
            (click)="cancel()"
            class="rounded-sm p-0.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200"
            aria-label="Cancel sending"
          >
            <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          </button>
        </div>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          It waits on that conversation's composer and goes with your next message there. You stay here.
        </p>

        @switch (r.phase) {
          @case ('loading') {
            <p class="mt-3 flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
              <app-spinner size="sm" />
              Preparing the result…
            </p>
          }
          @case ('error') {
            <p class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ r.error }}</p>
          }
          @default {
            <label [attr.for]="textId" class="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">Result</label>
            <textarea
              [id]="textId"
              rows="6"
              class="mt-1 block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              [ngModel]="r.text"
              (ngModelChange)="delivery.setSendBackText(sessionId(), $event)"
              [disabled]="r.phase === 'sending'"
            ></textarea>

            @if (r.created?.artifacts?.length) {
              <fieldset class="mt-3">
                <legend class="text-xs font-medium text-gray-700 dark:text-gray-300">Artifacts from this answer</legend>
                <ul class="mt-1 flex flex-col gap-1">
                  @for (a of r.created!.artifacts; track a.artifactId) {
                    <li>
                      <label class="flex items-center gap-2 text-sm/6 text-gray-800 dark:text-gray-200">
                        <input
                          type="checkbox"
                          class="size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500 dark:border-gray-600"
                          [checked]="r.artifactIds.includes(a.artifactId)"
                          (change)="delivery.toggleSendBackArtifact(sessionId(), a.artifactId, $any($event.target).checked)"
                          [disabled]="r.phase === 'sending'"
                        />
                        <span class="truncate">{{ a.title || 'Artifact' }}</span>
                      </label>
                    </li>
                  }
                </ul>
              </fieldset>
            }

            @if (r.error) {
              <p class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ r.error }}</p>
            }
          }
        }

        <div class="mt-4 flex items-center justify-end gap-2">
          <button
            type="button"
            (click)="cancel()"
            class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="send()"
            [disabled]="!canSend()"
            class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-medium text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            @if (r.phase === 'sending') {
              <app-spinner size="sm" />
            }
            Send to {{ sourceTitle() }}
            <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
          </button>
        </div>
      </section>
    }
  `,
})
export class SendBackSheetComponent {
  protected readonly delivery = inject(DeliveryService);
  private readonly state = inject(DeliveryStateService);
  private readonly sheet = viewChild<ElementRef<HTMLElement>>('sheet');

  /** The conversation the answer is in: the handoff's target. */
  readonly sessionId = input.required<string>();

  protected readonly textId = `send-back-text-${Math.random().toString(36).slice(2)}`;
  protected readonly review = computed(() => this.state.reviewFor(this.sessionId()));
  protected readonly sourceTitle = computed(
    () => this.review()?.created?.sourceTitle || 'the original conversation',
  );
  protected readonly canSend = computed(() => {
    const r = this.review();
    return !!r && r.phase === 'ready' && (r.text.trim().length > 0 || r.artifactIds.length > 0);
  });

  constructor() {
    // Bring the sheet into view and take focus when it opens, as the handoff review sheet does.
    effect(() => {
      const el = this.sheet()?.nativeElement;
      if (!el) return;
      el.scrollIntoView?.({ block: 'nearest' });
      el.focus({ preventScroll: true });
    });
  }

  protected send(): void {
    void this.delivery.sendBack(this.sessionId());
  }

  protected cancel(): void {
    if (this.review()) this.delivery.cancelSendBack(this.sessionId());
  }
}
