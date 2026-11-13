import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight, heroChevronRight, heroPaperClip, heroXMark } from '@ng-icons/heroicons/outline';

import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { formatFileSize } from '../../services/handoff/handoff-prompt';
import { MAX_SHARED_FILES } from '../../services/handoff/handoff.models';

/**
 * The review sheet a mid-thread `@`-mention opens (handoffs spec §4.2, step 2).
 *
 * Nothing is sent until the user presses Send. A labelled region above the composer, not a modal:
 * the thread the context comes from stays readable above it, which is what the user is judging the
 * card against. Esc cancels. The request is editable as a sentence; the context card is collapsed
 * by default and can be previewed in full, edited as a document, or removed.
 *
 * With a brief (handoffs spec §4.5.1), the card shows "Writing a brief…" and then the brief as it
 * streams; Send waits until it is written or has fallen back to the excerpt, so what the user
 * reviewed is what goes.
 */
@Component({
  selector: 'app-handoff-review-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroArrowRight, heroChevronRight, heroPaperClip, heroXMark })],
  host: { '(keydown.escape)': 'cancel()' },
  template: `
    @if (review(); as r) {
      <section
        #sheet
        role="region"
        [attr.aria-label]="'Hand off to ' + r.draft.agentName"
        tabindex="-1"
        class="mb-2 rounded-2xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
      >
        <div class="flex items-start justify-between gap-3">
          <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
            Hand off to {{ r.draft.agentName }}
          </h2>
          <button
            type="button"
            (click)="cancel()"
            class="rounded-sm p-0.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200"
            aria-label="Cancel handoff"
          >
            <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          </button>
        </div>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          {{ r.draft.agentName }} starts a new conversation in the background. You stay here.
        </p>

        <label [attr.for]="requestId" class="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">
          Request
        </label>
        <textarea
          [id]="requestId"
          rows="2"
          class="mt-1 block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          [ngModel]="r.request"
          (ngModelChange)="handoff.setRequest(sessionId(), $event)"
          [disabled]="r.phase === 'sending'"
        ></textarea>

        <div class="mt-3 rounded-lg bg-gray-50 px-3 py-2 dark:bg-white/5">
          @switch (r.phase) {
            @case ('loading') {
              <p class="flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
                <app-spinner size="sm" />
                Preparing context from this conversation…
              </p>
            }
            @case ('error') {
              <p class="text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ r.error }}</p>
            }
            @default {
              @if (r.brief?.status === 'writing') {
                @if (!r.brief?.text) {
                  <p class="flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400" role="status">
                    <app-spinner size="sm" />
                    Writing a brief…
                  </p>
                } @else {
                  <p class="text-sm/6 text-gray-700 dark:text-gray-300" role="status">Writing a brief…</p>
                  <!-- Scrollable, so focusable (axe scrollable-region-focusable). Not live: the status line says it's writing. -->
                  <pre tabindex="0" role="region" aria-label="Brief so far" aria-busy="true" class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ r.brief?.text }}</pre>
                }
                <button type="button" class="mt-2 text-sm/6 font-medium text-primary-accessible underline-offset-2 hover:underline" (click)="handoff.useExcerpt(sessionId())">
                  Use the conversation excerpt instead
                </button>
              } @else if (r.cardRemoved) {
                <p class="flex flex-wrap items-center gap-x-2 text-sm/6 text-gray-600 dark:text-gray-400">
                  No context from this conversation will be sent.
                  <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline" (click)="handoff.setCardRemoved(sessionId(), false)">
                    Add it back
                  </button>
                </p>
              } @else if (!r.card) {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">This conversation has no context to send yet.</p>
              } @else {
                <button
                  type="button"
                  class="flex w-full items-center gap-1.5 text-left text-sm/6 text-gray-700 dark:text-gray-300"
                  [attr.aria-expanded]="cardOpen()"
                  [attr.aria-controls]="cardId"
                  (click)="cardOpen.set(!cardOpen())"
                >
                  <ng-icon name="heroChevronRight" class="size-4 shrink-0 transition-transform" [class.rotate-90]="cardOpen()" aria-hidden="true" />
                  <span>
                    @if (r.brief?.status === 'written') {
                      Brief from this conversation
                    } @else {
                      Context from this conversation
                      @if (coverage(); as c) { · {{ c }} }
                    }
                    · ~{{ tokensLabel() }} tokens
                  </span>
                </button>
                @if (cardOpen()) {
                  <div [id]="cardId" class="mt-2">
                    @if (editing()) {
                      <label [attr.for]="cardEditorId" class="sr-only">Context card</label>
                      <textarea
                        [id]="cardEditorId"
                        rows="10"
                        class="block w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                        [ngModel]="r.card"
                        (ngModelChange)="handoff.setCard(sessionId(), $event)"
                      ></textarea>
                    } @else {
                      <!-- Scrollable, so focusable: keyboard users scroll it with the arrow keys (axe scrollable-region-focusable). -->
                      <pre tabindex="0" role="region" aria-label="Context card" class="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ r.card }}</pre>
                    }
                    <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm/6">
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline" (click)="previewOpen.set(!previewOpen())" [attr.aria-expanded]="previewOpen()" [attr.aria-controls]="previewId">
                        {{ previewOpen() ? 'Hide full prompt' : 'Preview full prompt' }}
                      </button>
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline" (click)="editing.set(!editing())">
                        {{ editing() ? 'Done editing' : 'Edit' }}
                      </button>
                      <button type="button" class="font-medium text-primary-accessible underline-offset-2 hover:underline" (click)="handoff.setCardRemoved(sessionId(), true)">
                        Remove
                      </button>
                    </div>
                  </div>
                }
              }
              @if (r.brief?.status === 'fallback' && !r.cardRemoved && r.card) {
                <p class="mt-1 text-xs text-gray-600 dark:text-gray-400">{{ fallbackNote() }}</p>
              }
              @if (previewOpen() && ((!r.cardRemoved && r.card) || r.fileIds.length) && r.brief?.status !== 'writing') {
                <div [id]="previewId" class="mt-2">
                  <p class="text-xs font-medium text-gray-700 dark:text-gray-300">What {{ r.draft.agentName }} will receive</p>
                  <pre tabindex="0" role="region" [attr.aria-label]="'Full prompt ' + r.draft.agentName + ' will receive'" class="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-2 font-mono text-xs text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-gray-800 dark:text-gray-200">{{ prompt() }}</pre>
                </div>
              }
            }
          }
        </div>

        @if (r.files?.length) {
          <fieldset class="mt-3">
            <legend class="text-xs font-medium text-gray-700 dark:text-gray-300">
              Files from this conversation
            </legend>
            <p [id]="filesHintId" class="text-xs text-gray-600 dark:text-gray-400">
              {{ r.draft.agentName }} can open the ones you tick. Up to {{ maxFiles }}.
            </p>
            <ul class="mt-1 space-y-1" [attr.aria-describedby]="filesHintId">
              @for (file of r.files; track file.uploadId) {
                <li>
                  <label class="flex items-center gap-2 text-sm/6 text-gray-800 dark:text-gray-200">
                    <input
                      type="checkbox"
                      class="size-4 rounded-sm border-gray-300 text-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600"
                      [checked]="r.fileIds.includes(file.uploadId)"
                      [disabled]="r.phase === 'sending' || (!r.fileIds.includes(file.uploadId) && r.fileIds.length >= maxFiles)"
                      (change)="toggleFile(file.uploadId, $event)"
                    />
                    <span class="min-w-0 truncate">{{ file.filename }}</span>
                    <span class="shrink-0 text-xs text-gray-500 dark:text-gray-400">{{ sizeLabel(file.sizeBytes) }}</span>
                  </label>
                </li>
              }
            </ul>
          </fieldset>
        }

        @if (attachmentCount() > 0) {
          <p class="mt-2 flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
            <ng-icon name="heroPaperClip" class="size-4" aria-hidden="true" />
            {{ attachmentCount() }} {{ attachmentCount() === 1 ? 'attachment goes' : 'attachments go' }} with the request
          </p>
        }

        <div class="mt-4 flex justify-end gap-2">
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
            class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Send to {{ r.draft.agentName }}
            <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
          </button>
        </div>
      </section>
    }
  `,
})
export class HandoffReviewSheetComponent {
  protected readonly handoff = inject(AgentHandoffService);
  private readonly state = inject(HandoffStateService);

  /** The source conversation: the one being viewed. */
  readonly sessionId = input.required<string>();

  private static nextId = 0;
  private readonly uid = HandoffReviewSheetComponent.nextId++;
  protected readonly requestId = `handoff-request-${this.uid}`;
  protected readonly cardId = `handoff-card-${this.uid}`;
  protected readonly cardEditorId = `handoff-card-editor-${this.uid}`;
  protected readonly previewId = `handoff-preview-${this.uid}`;
  protected readonly filesHintId = `handoff-files-hint-${this.uid}`;
  protected readonly maxFiles = MAX_SHARED_FILES;

  protected readonly cardOpen = signal(false);
  protected readonly editing = signal(false);
  protected readonly previewOpen = signal(false);

  private readonly sheet = viewChild<ElementRef<HTMLElement>>('sheet');

  protected readonly review = computed(() => this.state.reviewFor(this.sessionId()));

  protected readonly prompt = computed(() => {
    const r = this.review();
    return r ? this.handoff.previewPrompt(r) : '';
  });

  protected readonly coverage = computed(() => {
    const turns = this.review()?.created?.turnsCovered;
    if (!turns) return '';
    return turns.first === turns.last ? `turn ${turns.first}` : `turns ${turns.first}–${turns.last}`;
  });

  protected readonly tokensLabel = computed(() => {
    const r = this.review();
    if (!r) return '0';
    // Same estimate as the server: four characters a token.
    const tokens = Math.ceil(r.card.length / 4);
    return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  });

  protected readonly attachmentCount = computed(() => {
    const draft = this.review()?.draft;
    return (draft?.fileUploadIds?.length ?? 0) + (draft?.folders?.length ?? 0);
  });

  protected readonly canSend = computed(() => {
    const r = this.review();
    return !!r && r.phase === 'ready' && r.brief?.status !== 'writing' && r.request.trim().length > 0;
  });

  protected readonly fallbackNote = computed(() =>
    this.review()?.brief?.fallbackReason === 'preempted'
      ? 'The brief was stopped, so the conversation excerpt is shown instead.'
      : 'A brief couldn’t be written, so the conversation excerpt is shown instead.',
  );

  constructor() {
    // Move focus into the sheet when it opens, so Esc and the keyboard land here and a
    // screen reader hears the region's name.
    let focusedFor: string | undefined;
    let lastBriefStatus: string | undefined;
    effect(() => {
      const r = this.review();
      const el = this.sheet()?.nativeElement;
      const key = r ? `${this.sessionId()}:${r.draft.agentId}` : undefined;
      if (el && key && key !== focusedFor) {
        focusedFor = key;
        el.focus();
      }
      // A brief the user watched stream in stays open once written.
      const briefStatus = r?.brief?.status;
      if (briefStatus === 'written' && lastBriefStatus === 'writing') this.cardOpen.set(true);
      lastBriefStatus = briefStatus;
      if (!r) {
        focusedFor = undefined;
        this.cardOpen.set(false);
        this.editing.set(false);
        this.previewOpen.set(false);
      }
    });
  }

  protected cancel(): void {
    this.handoff.cancel(this.sessionId());
  }

  protected toggleFile(uploadId: string, event: Event): void {
    this.handoff.setFileShared(this.sessionId(), uploadId, (event.target as HTMLInputElement).checked);
  }

  protected sizeLabel(sizeBytes: number): string {
    return formatFileSize(sizeBytes);
  }

  protected send(): void {
    void this.handoff.send(this.sessionId());
  }
}
