import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheckCircle } from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { FEATURES } from '../../../services/features';
import { expiresIn } from '../../../services/notifications/needs-user';
import {
  AppNotification,
  InboxPrompt,
  NotificationResolution,
} from '../../../services/notifications/notification.model';
import { NotificationsService, inboxRefusalOf } from '../../../services/notifications/notifications.service';
import { relativePast } from '../../schedule-wording';
import { InboxPromptSlotComponent, InboxPromptView, InboxSlotAnswer, inboxPromptView } from './inbox-prompt-slot.component';

export interface InboxReviewDialogData {
  /** A waiting, answerable needs-user row. */
  notification: AppNotification;
}

/**
 * `undefined` means the dialog was closed with nothing sent.
 *
 * - `sent`: the answers were accepted (202); the run continues in the background.
 * - `settled`: the pause was answered somewhere else or expired (409 / 410), so the row reads so.
 * - `open`: the user chose to answer in the conversation.
 */
export type InboxReviewDialogResult =
  | { kind: 'sent'; resumeRunId: string; resolution: NotificationResolution }
  | { kind: 'settled'; state: 'answered_elsewhere' | 'expired'; message: string }
  | { kind: 'open' };

interface ReviewItem {
  prompt: InboxPrompt;
  view: InboxPromptView;
  /** Changes when the user changes the answer, so the slot (and its card) starts afresh. */
  key: string;
}

/** What an answered prompt is about, for its one-line summary: "Approved *send_message*". */
function subjectOf(view: InboxPromptView): string {
  switch (view.kind) {
    case 'approval':
      return view.request.toolName;
    case 'question':
      return 'the question';
    case 'form':
      return `${view.request.serverName || 'the server'}’s form`;
    case 'form_off':
      return `${view.serverName}’s form`;
    default:
      return '';
  }
}

/**
 * Review a paused scheduled run's prompts and answer them without opening its conversation
 * (answer-from-inbox §3.9).
 *
 * It reads the live prompts from the paused session's breadcrumbs (`GET /notifications/{id}/prompts`)
 * and renders each with the conversation's own card: an approval with View arguments for the full
 * input, the question picker, and an MCP server's form where `features.mcpElicitation` is on. All
 * the answers go in one `POST …/respond` (settled question 6): a single prompt sends on its own
 * answer, and several send together once each has one. It names the run's schedule and when the
 * pause expires, and always offers Open conversation.
 */
@Component({
  selector: 'app-inbox-review-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, DialogDescriptionDirective, InboxPromptSlotComponent, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroCheckCircle })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="title" size="lg" (closed)="cancel()">
      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">{{ description }}</p>

      @if (loading()) {
        <p class="sr-only" role="status">Loading what it’s asking…</p>
        <div aria-hidden="true" class="animate-pulse space-y-3 motion-reduce:animate-none">
          <div class="h-14 rounded-lg bg-gray-100 dark:bg-gray-700/50"></div>
        </div>
      } @else if (loadError(); as error) {
        <div role="alert" class="rounded-2xl border border-gray-200 px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:text-gray-200">
          {{ error }}
          <button
            type="button"
            (click)="load()"
            class="ml-1 font-medium text-primary-accessible underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
          >
            Try again
          </button>
        </div>
      } @else {
        <ul role="list" class="space-y-3" data-testid="inbox-review-prompts">
          @for (item of items(); track item.key) {
            <li>
              @if (answerFor(item); as answer) {
                <div class="flex flex-wrap items-center gap-2 rounded-2xl bg-gray-50 px-4 py-2.5 dark:bg-white/5" data-testid="inbox-answered">
                  <ng-icon name="heroCheckCircle" class="size-4 shrink-0 text-state-success-600 dark:text-state-success-400" aria-hidden="true" />
                  <p class="min-w-0 flex-1 text-sm/6 text-gray-900 dark:text-white">
                    {{ answer.word }}
                    @if (subject(item); as what) {
                      <span class="font-medium">{{ what }}</span>
                    }
                  </p>
                  @if (!single() && !sending()) {
                    <button
                      type="button"
                      (click)="change(item)"
                      class="rounded-2xl px-2.5 py-1 text-xs/5 font-medium text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:hover:bg-gray-700"
                    >
                      Change<span class="sr-only"> the answer for {{ subject(item) || 'this' }}</span>
                    </button>
                  }
                </div>
              } @else {
                <app-inbox-prompt-slot [view]="item.view" (answered)="onAnswered($event)" />
              }
            </li>
          }
        </ul>

        @if (sending()) {
          <p class="mt-3 flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-300" role="status">
            <app-spinner size="sm" label="Sending" />
            Sending your answer…
          </p>
        }
        @if (sendError(); as error) {
          <p role="alert" class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-400" data-testid="inbox-send-error">{{ error }}</p>
        }
        @if (blocked()) {
          <p class="mt-3 text-sm/6 text-gray-600 dark:text-gray-400" data-testid="inbox-blocked">
            Part of this can only be answered in the conversation.
          </p>
        }
      }

      <div dialogFooter class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="openConversation()"
          [disabled]="sending()"
          class="rounded-2xl px-1 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-primary-accessible-dark"
        >
          Open conversation
        </button>
        <div class="flex gap-3">
          <button
            type="button"
            (click)="cancel()"
            [disabled]="sending()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            {{ single() ? 'Close' : 'Cancel' }}
          </button>
          @if (!single() && items().length > 0) {
            <button
              type="button"
              (click)="send()"
              [disabled]="!ready() || sending()"
              data-testid="inbox-send"
              class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Send answers
            </button>
          }
        </div>
      </div>
    </app-dialog-shell>
  `,
})
export class InboxReviewDialogComponent {
  private readonly dialogRef = inject<DialogRef<InboxReviewDialogResult>>(DialogRef);
  private readonly data = inject<InboxReviewDialogData>(DIALOG_DATA);
  private readonly notifications = inject(NotificationsService);
  private readonly features = inject(FEATURES);

  private readonly notification = this.data.notification;
  private readonly label = this.notification.payload?.label?.trim() || 'A scheduled run';

  protected readonly title = `“${this.label}” is waiting for you`;
  protected readonly description = (() => {
    const pausedAt = this.notification.payload?.runFinishedAt || this.notification.createdAt;
    const when = relativePast(pausedAt);
    const expiry = expiresIn(this.notification.payload?.expiresAt);
    const parts = [
      when ? `Your scheduled run paused ${when} to ask you.` : 'Your scheduled run paused to ask you.',
      'Answer here and it carries on in the background.',
    ];
    if (expiry) parts.push(`${expiry}.`);
    return parts.join(' ');
  })();

  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  private readonly prompts = signal<InboxPrompt[]>([]);
  private readonly sessionId = signal('');
  private readonly answers = signal<ReadonlyMap<string, InboxSlotAnswer>>(new Map());
  private readonly generations = signal<ReadonlyMap<string, number>>(new Map());
  protected readonly sending = signal(false);
  protected readonly sendError = signal<string | null>(null);

  protected readonly items = computed<ReviewItem[]>(() => {
    const generations = this.generations();
    const formsEnabled = this.features.mcpElicitation;
    return this.prompts().map((prompt) => ({
      prompt,
      view: inboxPromptView(prompt, this.sessionId(), formsEnabled),
      key: `${prompt.interruptId}:${generations.get(prompt.interruptId) ?? 0}`,
    }));
  });

  /** One prompt sends on its own answer; several wait for Send. */
  protected readonly single = computed(() => this.items().length === 1);

  /** A prompt with no card here: the pause can only be answered in the conversation. */
  protected readonly blocked = computed(() => this.items().some((item) => item.view.kind === 'elsewhere'));

  protected readonly ready = computed(() => {
    const items = this.items();
    const answers = this.answers();
    return items.length > 0 && items.every((item) => answers.has(item.prompt.interruptId));
  });

  constructor() {
    void this.load();
  }

  protected async load(): Promise<void> {
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const page = await this.notifications.prompts(this.notification.notificationId);
      this.sessionId.set(page.sessionId);
      this.prompts.set(page.prompts);
    } catch (error) {
      const refusal = inboxRefusalOf(error);
      if (refusal.code === 'already_answered' || refusal.code === 'expired') {
        this.closeSettled(refusal.code, refusal.message);
        return;
      }
      this.loadError.set('What it’s asking couldn’t be loaded.');
    } finally {
      this.loading.set(false);
    }
  }

  protected answerFor(item: ReviewItem): InboxSlotAnswer | undefined {
    return this.answers().get(item.prompt.interruptId);
  }

  protected subject(item: ReviewItem): string {
    return subjectOf(item.view);
  }

  protected onAnswered(answer: InboxSlotAnswer): void {
    this.sendError.set(null);
    this.answers.update((map) => new Map(map).set(answer.interruptId, answer));
    if (this.single()) void this.send();
  }

  /** Take an answer back: the prompt's card comes back, empty. */
  protected change(item: ReviewItem): void {
    const id = item.prompt.interruptId;
    this.answers.update((map) => {
      const next = new Map(map);
      next.delete(id);
      return next;
    });
    this.generations.update((map) => new Map(map).set(id, (map.get(id) ?? 0) + 1));
  }

  protected async send(): Promise<void> {
    if (this.sending() || !this.ready()) return;
    this.sending.set(true);
    this.sendError.set(null);
    const answers = this.answers();
    const responses = this.prompts().map((prompt) => ({
      interruptId: prompt.interruptId,
      response: answers.get(prompt.interruptId)!.response,
    }));
    try {
      const accepted = await this.notifications.respond(this.notification.notificationId, responses);
      this.dialogRef.close({ kind: 'sent', resumeRunId: accepted.resumeRunId, resolution: accepted.resolution });
    } catch (error) {
      const refusal = inboxRefusalOf(error);
      if (refusal.code === 'already_answered' || refusal.code === 'expired') {
        this.closeSettled(refusal.code, refusal.message);
        return;
      }
      this.sendError.set(refusal.message);
      // One prompt has no Send button, so its card comes back to answer again.
      if (this.single()) this.change(this.items()[0]);
    } finally {
      this.sending.set(false);
    }
  }

  protected openConversation(): void {
    if (this.sending()) return;
    this.dialogRef.close({ kind: 'open' });
  }

  protected cancel(): void {
    // Closing mid-send would lose the run id the tracker needs; the send settles in a moment.
    if (this.sending()) return;
    this.dialogRef.close();
  }

  private closeSettled(code: 'already_answered' | 'expired', message: string): void {
    this.dialogRef.close({
      kind: 'settled',
      state: code === 'expired' ? 'expired' : 'answered_elsewhere',
      message,
    });
  }
}
