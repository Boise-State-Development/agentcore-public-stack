import { Component, ChangeDetectionStrategy, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { ReportReason } from '../models/store.model';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

export interface ReportAgentDialogData {
  agentName: string;
  /** True when the user already has an open report on this agent, so the copy can say so. */
  hasOpenReport?: boolean;
  /**
   * The conversation this was opened from, when it was opened from one.
   *
   * Presence is what switches the dialog into feedback framing and reveals the
   * attach-the-conversation checkbox — there is nothing to offer to attach without it.
   */
  sessionId?: string;
}

/** The reason + note (+ the conversation, if attached), or undefined if cancelled. */
export type ReportAgentDialogResult =
  | { reason: ReportReason; note?: string; sessionId?: string }
  | undefined;

/**
 * Feedback on an agent (D15) — "Report a problem" from the store page, "Send feedback"
 * from the foot of a conversation.
 *
 * The copy carries the one thing the user must not misread: **this is not a review**.
 * There is no rating here and none is coming — feedback is a private message to the
 * people who curate the store, it never appears on the agent's page, and the author never
 * learns who sent it. A user who thinks they are posting a public review will write a
 * different (and less useful) thing than one who knows they are filing a ticket.
 *
 * The reason set is fixed so the queue can be triaged by severity without reading every
 * note. The labels say what each one *means to the reader*, not what it is called in the
 * enum — "It gives wrong answers" is answerable; "inaccurate" is a category.
 *
 * **One dialog, two entry points, deliberately.** The conversation entry point could have
 * had its own component with warmer copy, and then there would be two reason lists to keep
 * in step with one backend enum. What actually differs is the title, the lead-in, and
 * whether a conversation is on offer — so those are the only things `sessionId` changes.
 */
@Component({
  selector: 'app-report-agent-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <!-- Dismiss-on-click goes here, not on the backdrop — see DialogDismissDirective. -->
    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()"
    >
      <!--
        Capped and column-flexed so the body scrolls and the header/footer stay put,
        matching the sibling dialogs in this folder. Without the cap the panel grows past
        the viewport and simply clips: the title goes off the top and Send goes off the
        bottom, with nothing to scroll. This one got away with it while it was four
        reasons and no checkbox; adding the suggestion reason and the conversation opt-in
        is what pushed it over on a short window.
      -->
      <div
        class="dialog-panel relative flex max-h-[90vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-title"
        aria-describedby="report-description"
      >
        <div class="flex shrink-0 items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="report-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              @if (fromConversation()) {
                Feedback on “{{ data.agentName }}”
              } @else {
                Report a problem with “{{ data.agentName }}”
              }
            </h2>
            <p id="report-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              This goes privately to the people who curate the store. It is not a review —
              it never appears on this agent's page, and the author is never told who sent it.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          @if (data.hasOpenReport) {
            <!-- D15.4 surfaced before the user writes, not after: they are amending. -->
            <div class="mb-4 rounded-2xl bg-amber-50 px-4 py-3 dark:bg-amber-900/20">
              <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                You already have a report open on this agent. Sending this will
                <strong>update</strong> it rather than adding a second one.
              </p>
            </div>
          }

          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">
              @if (fromConversation()) {
                What kind of feedback?
              } @else {
                What's wrong?
              }
            </legend>
            <div class="mt-2 flex flex-col gap-2">
              @for (option of reasons; track option.value) {
                <label
                  class="flex cursor-pointer items-start gap-3 rounded-2xl border px-4 py-3 text-sm/6"
                  [class]="
                    reason() === option.value
                      ? 'border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-900/20'
                      : 'border-gray-300 bg-white hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-gray-700'
                  "
                >
                  <input
                    type="radio"
                    name="report-reason"
                    class="mt-1 size-4 shrink-0 accent-blue-600"
                    [value]="option.value"
                    [checked]="reason() === option.value"
                    (change)="reason.set(option.value)"
                  />
                  <span class="min-w-0">
                    <span class="block font-medium text-gray-900 dark:text-white">
                      {{ option.label }}
                    </span>
                    <span class="block text-gray-500 dark:text-gray-400">{{ option.hint }}</span>
                  </span>
                </label>
              }
            </div>
          </fieldset>

          <label
            for="report-note"
            class="mt-4 block text-sm/6 font-medium text-gray-900 dark:text-white"
          >
            Anything else? <span class="font-normal text-gray-500">(optional)</span>
          </label>
          <textarea
            id="report-note"
            rows="3"
            maxlength="2000"
            [value]="note()"
            (input)="onNoteInput($event)"
            placeholder="What did you ask, and what happened?"
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>

          <!-- Only offered when there is a conversation to offer. Ticked by default
               because it is what makes the feedback actionable, but it is the user's
               conversation, so it stays a choice they can see and undo. -->
          @if (fromConversation()) {
            <label
              class="mt-4 flex cursor-pointer items-start gap-3 rounded-2xl border border-gray-300 bg-white px-4 py-3 text-sm/6 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-gray-700"
            >
              <input
                type="checkbox"
                class="mt-1 size-4 shrink-0 accent-blue-600"
                [checked]="attachConversation()"
                (change)="onAttachToggle($event)"
              />
              <span class="min-w-0">
                <span class="block font-medium text-gray-900 dark:text-white">
                  Include a reference to this conversation
                </span>
                <span class="block text-gray-500 dark:text-gray-400">
                  Lets the curators look up what happened. Only they can see it.
                </span>
              </span>
            </label>
          }
        </div>

        <div
          class="flex shrink-0 justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            [disabled]="!reason()"
            (click)="onSubmit()"
            class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            {{ submitLabel() }}
          </button>
        </div>
      </div>
    </div>
  `,
})
export class ReportAgentDialogComponent {
  private dialogRef = inject<DialogRef<ReportAgentDialogResult>>(DialogRef);
  readonly data = inject<ReportAgentDialogData>(DIALOG_DATA);

  /** Ordered by how often they are the right answer, not by severity. */
  readonly reasons: { value: ReportReason; label: string; hint: string }[] = [
    { value: 'inaccurate', label: 'It gives wrong answers', hint: 'The information it returns is incorrect or out of date.' },
    { value: 'broken', label: "It doesn't work", hint: 'It errors, hangs, or ignores what it is asked.' },
    { value: 'inappropriate', label: 'It produced something inappropriate', hint: 'Offensive, unsafe, or content that should not be here.' },
    { value: 'suggestion', label: 'It could do more', hint: 'Something it should be able to do, but cannot yet.' },
    { value: 'other', label: 'Something else', hint: 'Tell us below.' },
  ];

  readonly reason = signal<ReportReason | null>(null);
  readonly note = signal('');

  /** Ticked by default — see the checkbox comment in the template. */
  readonly attachConversation = signal(true);

  /** Whether this was opened from a conversation, which is the only thing `sessionId` decides. */
  readonly fromConversation = computed(() => !!this.data.sessionId);

  readonly submitLabel = computed(() => {
    if (this.data.hasOpenReport) return 'Update feedback';
    return this.fromConversation() ? 'Send feedback' : 'Send report';
  });

  onNoteInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  onAttachToggle(event: Event): void {
    this.attachConversation.set((event.target as HTMLInputElement).checked);
  }

  onSubmit(): void {
    const reason = this.reason();
    if (!reason) return;
    this.dialogRef.close({
      reason,
      note: this.note().trim() || undefined,
      // Unticking must send *nothing*, not a falsy flag alongside the id — the backend
      // treats an absent sessionId on an amendment as withdrawing what was shared before.
      sessionId: this.attachConversation() ? this.data.sessionId : undefined,
    });
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
