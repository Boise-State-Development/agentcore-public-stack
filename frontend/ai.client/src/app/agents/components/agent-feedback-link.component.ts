import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChatBubbleLeftRight, heroCheckCircle } from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import { AgentApiService } from '../services/agent-api.service';
import {
  ReportAgentDialogComponent,
  ReportAgentDialogData,
  ReportAgentDialogResult,
} from './report-agent-dialog.component';

/**
 * "Give feedback on <agent>" at the foot of a conversation (D15).
 *
 * The store's detail page has carried this since Phase 8, and almost nobody used it —
 * because the moment you *have* something to say about an agent is the moment it just
 * answered you, not a later trip back to the tile you launched it from. So the same
 * intake now sits where the reaction happens.
 *
 * **Self-contained on purpose.** The dialog, the POST, and the confirmation all live here
 * rather than bubbling an output up through the message list and chat container to the
 * session page. Nothing above needs to know feedback was sent — no session state changes,
 * no message is added — so routing it through three components would be plumbing that
 * exists only to come back to the same place.
 *
 * ⚠️ **Rendered only for a published agent**, mirroring `agent-detail.page.ts`. The backend
 * gate is the real one (D15.3: you may report what the store offered you); this just keeps
 * us from showing a link whose only possible outcome is a 400.
 */
@Component({
  selector: 'app-agent-feedback-link',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroChatBubbleLeftRight, heroCheckCircle })],
  template: `
    <!--
      Right-aligned and quiet: this is a footnote to the conversation, not a call to
      action, and centring it under the last answer gave it the weight of one.

      ⚠️ Subtlety here is size and weight, NOT contrast. The obvious way to play this down
      is a lighter grey, but gray-400 on white is ~2.8:1 and fails AA for body text — so
      the resting colour stays gray-500 (~4.8:1) and the smaller type, dropped hover fill
      and column layout do the work instead.
    -->
    <div class="mt-4 flex flex-col items-end gap-1 px-4 pb-2">
      @if (confirmation(); as message) {
        <p
          class="flex items-center gap-1.5 text-xs/5 text-gray-500 dark:text-gray-400"
          role="status"
        >
          <ng-icon
            name="heroCheckCircle"
            class="size-3.5 shrink-0 text-state-success-600 dark:text-state-success-400"
            aria-hidden="true"
          />
          <span>{{ message }}</span>
        </p>
      } @else {
        <button
          type="button"
          [disabled]="busy()"
          (click)="onOpen()"
          class="flex items-center gap-1.5 rounded-2xl px-2 py-1 text-xs/5 text-gray-500 hover:text-gray-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:text-gray-200"
        >
          <ng-icon name="heroChatBubbleLeftRight" class="size-3.5 shrink-0" aria-hidden="true" />
          <span>Give feedback on {{ agentName() }}</span>
        </button>
      }

      @if (error(); as message) {
        <p class="text-xs/5 text-state-danger-600 dark:text-state-danger-400" role="alert">{{ message }}</p>
      }
    </div>
  `,
})
export class AgentFeedbackLinkComponent {
  private readonly dialog = inject(Dialog);
  private readonly api = inject(AgentApiService);

  readonly agentId = input.required<string>();
  readonly agentName = input.required<string>();

  /** The conversation to offer to attach. Null before the session id settles. */
  readonly sessionId = input<string | null>(null);

  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  /**
   * Session-local, exactly as on the detail page: there is no read endpoint for "have I
   * given feedback on this", and the one-open-report rule (D15.4) means a second send
   * amends the first. Knowing that within this page is enough to word the confirmation
   * honestly; a reload starting fresh only costs the user a slightly wrong verb.
   */
  protected readonly confirmation = signal<string | null>(null);
  private readonly hasOpenReport = signal(false);

  private readonly dialogData = computed<ReportAgentDialogData>(() => ({
    agentName: this.agentName(),
    hasOpenReport: this.hasOpenReport(),
    sessionId: this.sessionId() ?? undefined,
  }));

  async onOpen(): Promise<void> {
    const ref = this.dialog.open<ReportAgentDialogResult, ReportAgentDialogData>(
      ReportAgentDialogComponent,
      { data: this.dialogData() },
    );

    const result = await firstValueFrom(ref.closed);
    if (!result) return;

    this.busy.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.reportAgent(this.agentId(), result));
      this.hasOpenReport.set(true);
      // Telling someone their feedback was "sent" when it amended an earlier one would
      // leave them expecting two things in the queue (D15.4).
      this.confirmation.set(
        response.replacedExisting
          ? 'Thanks — we updated the feedback you already had open on this agent.'
          : 'Thanks — your feedback went to the people who curate the store.',
      );
    } catch {
      this.error.set('Failed to send this feedback.');
    } finally {
      this.busy.set(false);
    }
  }
}
