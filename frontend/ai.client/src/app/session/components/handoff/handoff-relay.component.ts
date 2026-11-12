import { ChangeDetectionStrategy, Component, computed, effect, inject, input, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';

import { OAuthConsentRequest } from '../../../services/oauth-consent/oauth-consent.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { ToolApprovalRequest } from '../../../services/tool-approval/tool-approval.service';
import { UserQuestionRequest } from '../../../services/user-question/user-question.service';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffRelayService } from '../../services/handoff/handoff-relay.service';
import { HandoffView, RelayedPrompt } from '../../services/handoff/handoff.models';
import { OAuthConsentPromptComponent } from '../message-list/components/oauth-consent-prompt/oauth-consent-prompt.component';
import { ToolApprovalPromptComponent } from '../message-list/components/tool-approval-prompt/tool-approval-prompt.component';
import { UserQuestionPromptComponent } from '../message-list/components/user-question-prompt/user-question-prompt.component';

interface RelayItems {
  consents: OAuthConsentRequest[];
  approvals: ToolApprovalRequest[];
  questions: UserQuestionRequest[];
  signIn: boolean;
  /** Every prompt the user can see, for the shown count. */
  visible: RelayedPrompt[];
}

/**
 * A handoff target's prompt, relayed under its row in the source (handoffs spec §4.3, PR-0.3c).
 *
 * The prompts are the target's, and the block says so: it names the Agent, sits inside that
 * handoff's row, and is never the thread's own prompt slot. Each prompt is the one the target's
 * thread would show (the same consent, approval and question components, bound to the target's
 * session), so answering here resumes the target in the background and the user stays put:
 *
 * - **OAuth consent:** Connect. **Tool approval:** Approve / Deny, with the tool and its arguments.
 * - **`ask_user_question`:** the picker, plus Open conversation (some answers need the target's
 *   context, so the link is never hidden).
 * - **Browser sign-in:** Open conversation only. Its live view is the target's screen.
 *
 * It never interrupts a running source turn: while the source streams, the row's amber mark shows
 * and this waits. Once answered it collapses to one line ("Connected · *Agent* resumed", or
 * "Already answered" when the other surface was first).
 */
@Component({
  selector: 'app-handoff-relay',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, OAuthConsentPromptComponent, ToolApprovalPromptComponent, UserQuestionPromptComponent],
  template: `
    @if (items(); as relay) {
      <section
        class="mt-2 space-y-2 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-4 py-3 dark:border-state-warning-800 dark:bg-state-warning-900/20"
        [attr.aria-label]="'Waiting on you, from ' + row().agentName"
        data-testid="handoff-relay"
      >
        <p class="text-sm/6 text-gray-900 dark:text-gray-100">{{ headline() }}</p>
        @for (request of relay.consents; track request.providerId) {
          <app-oauth-consent-prompt [request]="request" />
        }
        @for (request of relay.approvals; track request.interruptId) {
          <app-tool-approval-prompt [request]="request" />
        }
        @for (request of relay.questions; track request.interruptId) {
          <app-user-question-prompt [request]="request" />
        }
        @if (link(); as l) {
          <a
            [routerLink]="l.commands"
            [queryParams]="l.queryParams"
            class="inline-block rounded-md text-xs/5 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
          >
            Open conversation
          </a>
        }
      </section>
    } @else if (notice(); as n) {
      <p
        class="mt-1.5 px-4 text-xs/5"
        [class]="n.tone === 'success' ? 'text-state-success-700 dark:text-state-success-400' : 'text-gray-600 dark:text-gray-400'"
        role="status"
        data-testid="handoff-relay-notice"
      >
        {{ n.text }}
      </p>
    }
  `,
})
export class HandoffRelayComponent {
  private readonly relay = inject(HandoffRelayService);
  private readonly chatState = inject(ChatStateService);
  private readonly handoff = inject(AgentHandoffService);
  private readonly connectors = inject(UserConnectorsService);

  readonly row = input.required<HandoffView>();

  protected readonly link = computed(() => this.handoff.targetLink(this.row()));

  protected readonly notice = computed(() => this.relay.notice(this.row().handoffId));

  /** What to show, or null: nothing waiting, nothing still answerable, or the source mid-turn. */
  protected readonly items = computed<RelayItems | null>(() => {
    const row = this.row();
    if (!this.relay.isWaiting(row)) return null;
    // A relay never lands in the middle of the source's own reply (the amber mark doesn't wait).
    if (this.chatState.isSessionLoading(row.sessionId)) return null;
    const items: RelayItems = { consents: [], approvals: [], questions: [], signIn: false, visible: [] };
    for (const prompt of row.relayedPrompts ?? []) {
      switch (prompt.kind) {
        case 'oauth': {
          const request = this.relay.consentFor(row, prompt);
          // One connection answers every consent waiting on that provider.
          if (request && !items.consents.some((r) => r.providerId === request.providerId)) {
            items.consents.push(request);
            items.visible.push(prompt);
          }
          break;
        }
        case 'tool_approval': {
          const request = this.relay.approvalFor(row, prompt);
          if (request) {
            items.approvals.push(request);
            items.visible.push(prompt);
          }
          break;
        }
        case 'user_question': {
          const request = this.relay.questionFor(row, prompt);
          if (request) {
            items.questions.push(request);
            items.visible.push(prompt);
          }
          break;
        }
        case 'browser_login':
          items.signIn = true;
          items.visible.push(prompt);
          break;
      }
    }
    return items.visible.length > 0 ? items : null;
  });

  protected readonly headline = computed(() => {
    const items = this.items();
    const agent = this.row().agentName;
    if (!items) return '';
    const kinds = new Set(items.visible.map((p) => p.kind));
    if (kinds.size > 1) return `${agent} needs your input to keep going.`;
    if (items.consents.length > 0) {
      const names = items.consents.map((r) => this.connectors.displayNameFor(r.providerId));
      return `${agent} needs you to connect ${joinNames(names)} to keep going.`;
    }
    if (items.approvals.length > 0) return `${agent} needs your approval to keep going.`;
    if (items.questions.length > 0) return `${agent} has a question for you.`;
    return `${agent} needs you to sign in to keep going.`;
  });

  constructor() {
    effect(() => {
      const items = this.items();
      const row = this.row();
      if (!items) return;
      untracked(() => {
        this.relay.clearNotice(row.handoffId);
        for (const prompt of items.visible) this.relay.markShown(row, prompt);
      });
    });
  }
}

function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? 'an account';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}
