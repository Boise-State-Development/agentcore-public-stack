import { ChangeDetectionStrategy, Component, OnInit, computed, inject, input, output } from '@angular/core';
import { InboxPrompt } from '../../../services/notifications/notification.model';
import {
  McpElicitationRequest,
  McpElicitationResponse,
  McpElicitationService,
} from '../../../services/mcp-elicitation/mcp-elicitation.service';
import { ToolApprovalRequest, ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import {
  UserQuestionRequest,
  UserQuestionResponse,
  UserQuestionService,
} from '../../../services/user-question/user-question.service';
import { validateMcpElicitationRequiredEvent, validateUserQuestions } from '../../../shared/utils/stream-parser';
import { McpElicitationPromptComponent } from '../../../session/components/message-list/components/mcp-elicitation-prompt/mcp-elicitation-prompt.component';
import { ToolApprovalPromptComponent } from '../../../session/components/message-list/components/tool-approval-prompt/tool-approval-prompt.component';
import { UserQuestionPromptComponent } from '../../../session/components/message-list/components/user-question-prompt/user-question-prompt.component';

/** One prompt's answer, as the dialog collects it: the response the card would post, and a word for it. */
export interface InboxSlotAnswer {
  interruptId: string;
  response: unknown;
  /** "Approved", "Declined", "Answered", "Skipped", "Submitted", "Dismissed". */
  word: string;
}

/**
 * How one live prompt renders in the review dialog: with the conversation's own card, or, where
 * no card can answer it here, a line saying where it can be answered.
 */
export type InboxPromptView =
  | { kind: 'approval'; request: ToolApprovalRequest }
  | { kind: 'question'; request: UserQuestionRequest }
  | { kind: 'form'; request: McpElicitationRequest }
  | { kind: 'form_off'; interruptId: string; serverName: string }
  | { kind: 'elsewhere'; reason: string };

function parseJson(text: string | null | undefined): unknown {
  try {
    return JSON.parse(text ?? '');
  } catch {
    return undefined;
  }
}

/**
 * Turn a breadcrumb into the request its card takes, through the same validators the stream and
 * a reload use: a prompt that would not render in the conversation does not render here either,
 * and is answered there instead.
 */
export function inboxPromptView(prompt: InboxPrompt, sessionId: string, formsEnabled: boolean): InboxPromptView {
  switch (prompt.kind) {
    case 'tool_approval':
      return {
        kind: 'approval',
        request: {
          interruptId: prompt.interruptId,
          toolUseId: prompt.toolUseId ?? '',
          toolName: prompt.toolName || 'a tool',
          toolInput: prompt.toolInput ?? undefined,
          message: prompt.message ?? '',
          receivedAt: 0,
          sessionId,
        },
      };
    case 'user_question': {
      const questions = parseJson(prompt.questions);
      if (!validateUserQuestions(questions)) return { kind: 'elsewhere', reason: 'This question opens in the conversation.' };
      return {
        kind: 'question',
        request: { interruptId: prompt.interruptId, toolUseId: prompt.toolUseId ?? '', questions, receivedAt: 0, sessionId },
      };
    }
    case 'mcp_elicitation': {
      const form = parseJson(prompt.elicitation);
      if (!validateMcpElicitationRequiredEvent(form) || form.fields.length === 0) {
        return { kind: 'elsewhere', reason: 'This form opens in the conversation.' };
      }
      if (!formsEnabled) {
        return { kind: 'form_off', interruptId: prompt.interruptId, serverName: form.serverName || 'An MCP server' };
      }
      return {
        kind: 'form',
        request: {
          interruptId: prompt.interruptId,
          toolUseId: prompt.toolUseId ?? form.toolUseId,
          toolName: form.toolName,
          serverName: form.serverName,
          message: form.message,
          fields: form.fields,
          receivedAt: 0,
          sessionId,
        },
      };
    }
    case 'browser_login':
      return { kind: 'elsewhere', reason: 'Signing in to a site happens in the conversation, where its screen is.' };
    default:
      return { kind: 'elsewhere', reason: 'Connecting an account happens in the conversation.' };
  }
}

/**
 * One prompt of a paused run, answered with the conversation's own card (answer-from-inbox §3.9).
 *
 * The cards resume a turn through their services' resume handlers. Here each slot provides its own
 * instances of those services, whose handlers only hand the answer up: the dialog posts every
 * answer together, in one resume (settled question 6). A slot is created afresh to change an
 * answer, so its services start empty.
 */
@Component({
  selector: 'app-inbox-prompt-slot',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ToolApprovalPromptComponent, UserQuestionPromptComponent, McpElicitationPromptComponent],
  providers: [ToolApprovalService, UserQuestionService, McpElicitationService],
  host: { class: 'block' },
  template: `
    @switch (view().kind) {
      @case ('approval') {
        @if (approval(); as request) {
          <app-tool-approval-prompt [request]="request" />
        }
      }
      @case ('question') {
        @if (question(); as request) {
          <app-user-question-prompt [request]="request" />
        }
      }
      @case ('form') {
        @if (form(); as request) {
          <app-mcp-elicitation-prompt [request]="request" />
        }
      }
      @case ('form_off') {
        <div
          class="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-gray-200 px-4 py-3 dark:border-gray-700"
          data-testid="inbox-form-off"
        >
          <p class="text-sm/6 text-gray-700 dark:text-gray-200">{{ formOffLine() }}</p>
          <button
            type="button"
            (click)="declineForm()"
            class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Decline
          </button>
        </div>
      }
      @default {
        <p
          class="rounded-2xl border border-gray-200 px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:text-gray-200"
          data-testid="inbox-prompt-elsewhere"
        >
          {{ elsewhereLine() }}
        </p>
      }
    }
  `,
})
export class InboxPromptSlotComponent implements OnInit {
  private readonly approvals = inject(ToolApprovalService);
  private readonly questions = inject(UserQuestionService);
  private readonly forms = inject(McpElicitationService);

  readonly view = input.required<InboxPromptView>();
  readonly answered = output<InboxSlotAnswer>();

  protected readonly approval = computed(() => this.pendingFor(this.approvals.pending()));
  protected readonly question = computed(() => this.pendingFor(this.questions.pending()));
  protected readonly form = computed(() => this.pendingFor(this.forms.pending()));

  protected readonly formOffLine = computed(() => {
    const view = this.view();
    const server = view.kind === 'form_off' ? view.serverName : 'An MCP server';
    return `${server} asks you to fill in a form. Forms open in the conversation, or you can decline it here.`;
  });

  protected readonly elsewhereLine = computed(() => {
    const view = this.view();
    return view.kind === 'elsewhere' ? view.reason : '';
  });

  constructor() {
    this.approvals.setResumeHandler((interruptId, decision) =>
      this.answered.emit({ interruptId, response: decision, word: decision === 'approved' ? 'Approved' : 'Declined' }),
    );
    this.questions.setResumeHandler((interruptId, response: UserQuestionResponse) =>
      this.answered.emit({ interruptId, response, word: 'skipped' in response ? 'Skipped' : 'Answered' }),
    );
    this.forms.setResumeHandler((interruptId, response: McpElicitationResponse) =>
      this.answered.emit({ interruptId, response, word: formWord(response) }),
    );
  }

  ngOnInit(): void {
    const view = this.view();
    switch (view.kind) {
      case 'approval':
        this.approvals.requestApproval(view.request);
        break;
      case 'question':
        this.questions.requestAnswers(view.request);
        break;
      case 'form':
        this.forms.requestInput(view.request);
        break;
    }
  }

  /** A form this build doesn't show can still be declined: it was never registered, so this answers it. */
  protected declineForm(): void {
    const view = this.view();
    if (view.kind !== 'form_off') return;
    this.answered.emit({ interruptId: view.interruptId, response: { action: 'decline' }, word: 'Declined' });
  }

  private pendingFor<T extends { interruptId: string }>(pending: T[]): T | undefined {
    const view = this.view();
    if (!('request' in view)) return undefined;
    return pending.find((r) => r.interruptId === view.request.interruptId);
  }
}

function formWord(response: McpElicitationResponse): string {
  switch (response.action) {
    case 'accept':
      return 'Submitted';
    case 'decline':
      return 'Declined';
    default:
      return 'Dismissed';
  }
}
