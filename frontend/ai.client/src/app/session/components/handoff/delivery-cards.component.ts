import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowUturnLeft, heroXMark } from '@ng-icons/heroicons/outline';

import { ComposerCard, DELIVERY_MAX_PER_TURN, DeliveryStateService } from '../../services/handoff/delivery-state.service';
import { DeliveryService } from '../../services/handoff/delivery.service';
import { DeliveryArtifactChipsComponent } from './delivery-artifact-chips.component';
import { DeliveryFileChipsComponent } from './delivery-file-chips.component';

/**
 * Cards waiting on this conversation's composer (handoffs spec §4.8): results sent back from
 * another of the user's conversations. Each rides the next send unless removed, and the user can
 * read it in full or edit it first. Nothing here sends; the composer's Send does.
 */
@Component({
  selector: 'app-delivery-cards',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, DeliveryArtifactChipsComponent, DeliveryFileChipsComponent],
  providers: [provideIcons({ heroArrowUturnLeft, heroXMark })],
  template: `
    @if (cards().length > 0) {
      <section
        role="region"
        aria-label="Results to send with your next message"
        class="mb-2 flex flex-col gap-2"
      >
        @for (card of cards(); track card.deliveryId; let i = $index) {
          <div
            class="rounded-2xl border border-gray-200 bg-white px-4 py-3 shadow-sm dark:border-gray-700 dark:bg-gray-900"
            [class.opacity-70]="i >= maxPerSend"
          >
            <div class="flex items-start justify-between gap-3">
              <p class="flex min-w-0 items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <ng-icon name="heroArrowUturnLeft" class="size-3.5 shrink-0" aria-hidden="true" />
                <span class="truncate">From <span class="font-medium text-gray-900 dark:text-white">{{ card.fromTitle || 'another conversation' }}</span></span>
                @if (card.editedText !== undefined) {
                  <span class="shrink-0 rounded-full bg-gray-100 px-1.5 text-[11px] text-gray-700 dark:bg-white/10 dark:text-gray-300">Edited</span>
                }
              </p>
              <button
                type="button"
                (click)="remove(card)"
                class="-mr-1 shrink-0 rounded-sm p-0.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200"
                [attr.aria-label]="'Remove the result from ' + (card.fromTitle || 'another conversation')"
              >
                <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
              </button>
            </div>

            @if (editingId() === card.deliveryId) {
              <label [attr.for]="'delivery-edit-' + card.deliveryId" class="sr-only">Result to send</label>
              <textarea
                [id]="'delivery-edit-' + card.deliveryId"
                rows="6"
                class="mt-2 block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                [ngModel]="card.editedText ?? card.text"
                (ngModelChange)="state.edit(sessionId(), card.deliveryId, $event)"
              ></textarea>
            } @else if (openId() === card.deliveryId) {
              <!-- Scrollable, so focusable (axe scrollable-region-focusable). -->
              <pre tabindex="0" role="region" [attr.aria-label]="'Result from ' + (card.fromTitle || 'another conversation')" class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-2 text-sm/6 text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-white/5 dark:text-gray-200">{{ card.editedText ?? card.text }}</pre>
            } @else if (card.editedText ?? card.text) {
              <p class="mt-1 truncate text-sm/6 text-gray-800 dark:text-gray-200">{{ card.editedText ?? card.text }}</p>
            }

            @if (card.artifacts.length > 0) {
              <div class="mt-2">
                <app-delivery-artifact-chips [artifacts]="card.artifacts" [label]="'Artifacts from ' + (card.fromTitle || 'another conversation')" />
              </div>
            }

            @if (card.files?.length) {
              <!-- Shared by reference (§4.9): the chip downloads the original until the send makes it this conversation's. -->
              <div class="mt-2">
                <app-delivery-file-chips [files]="card.files!" [label]="'Files from ' + (card.fromTitle || 'another conversation')" />
              </div>
            }

            <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs/5">
              @if (editingId() !== card.deliveryId) {
                <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline dark:text-primary-50" [attr.aria-expanded]="openId() === card.deliveryId" (click)="toggleOpen(card.deliveryId)">
                  {{ openId() === card.deliveryId ? 'Hide' : 'Preview' }}
                </button>
              }
              <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline dark:text-primary-50" (click)="toggleEdit(card.deliveryId)">
                {{ editingId() === card.deliveryId ? 'Done editing' : 'Edit' }}
              </button>
              <span class="text-gray-500 dark:text-gray-400">
                {{ i < maxPerSend ? 'Goes with your next message' : 'Goes with the message after' }}
              </span>
            </div>
          </div>
        }
      </section>
    }
  `,
})
export class DeliveryCardsComponent {
  protected readonly state = inject(DeliveryStateService);
  private readonly delivery = inject(DeliveryService);

  readonly sessionId = input.required<string>();

  protected readonly maxPerSend = DELIVERY_MAX_PER_TURN;
  protected readonly cards = computed(() => this.state.cardsFor(this.sessionId()));
  protected readonly openId = signal<string | null>(null);
  protected readonly editingId = signal<string | null>(null);

  protected toggleOpen(deliveryId: string): void {
    this.openId.set(this.openId() === deliveryId ? null : deliveryId);
  }

  protected toggleEdit(deliveryId: string): void {
    this.editingId.set(this.editingId() === deliveryId ? null : deliveryId);
  }

  protected remove(card: ComposerCard): void {
    void this.delivery.dismissCard(this.sessionId(), card);
  }
}
