import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  input,
  output,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowsPointingIn,
  heroArrowsPointingOut,
  heroBeaker,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { ChatContainerComponent, ChatContainerConfig } from '../../../session/components/chat-container/chat-container.component';
import { ChatInputComponent } from '../../../session/components/chat-input/chat-input.component';
import { PreviewChatService } from '../../../assistants/assistant-form/services/preview-chat.service';

/**
 * Test-drive the submission under review, before deciding on it.
 *
 * **The gap this closes.** A reviewer could read a name and a category and, once the
 * submission review page arrived, a system prompt — but had no way to find out what the
 * agent actually *does*. `/assistants/{id}/test-chat` is not that harness: it is
 * owner/editor-only, needs processed documents, and passes `enabled_tools=None`, so it
 * exercises none of the agent's bindings. Chatting with the agent normally is not it
 * either, for a subtler reason — `resolve_invocation_agent` hands a non-owner the
 * *published* snapshot or the live draft, so a reviewer would be test-driving anything
 * except the version they are about to approve.
 *
 * So this streams through the same real invocation path the main chat uses, with
 * `reviewPreview` set. Server-side that resolves the agent to the snapshot under review
 * (`resolve_review_agent`) and bypasses the PRIVATE visibility check — a PRIVATE agent can
 * be sitting in the queue, and an ordinary read 403s on one. The scope is re-checked
 * against the caller's own roles there, so this flag cannot widen anything on its own.
 *
 * ⚠️ **It runs under the reviewer's identity, and that is worth saying out loud on the
 * page.** Binding resolution is RBAC-filtered per invoker, so a tool the reviewer lacks
 * behaves differently for them than for the users who will pin this. The banner says so
 * rather than letting a clean test drive read as proof it will run for everyone.
 *
 * The `preview-` session id keeps the whole exchange out of the author's conversation
 * history, and the invocation path skips its bookkeeping writes (`lastUsedAt`, share
 * interaction) for a review preview — a reviewer poking a submission is not use.
 */
@Component({
  selector: 'app-review-test-drive',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ChatContainerComponent, ChatInputComponent],
  providers: [
    PreviewChatService,
    provideIcons({
      heroArrowsPointingIn,
      heroArrowsPointingOut,
      heroBeaker,
      heroExclamationTriangle,
    }),
  ],
  template: `
    <div class="flex h-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
      <div class="shrink-0 border-b border-gray-200 px-4 py-3 dark:border-gray-700">
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <h3 class="flex items-center gap-1.5 text-sm/6 font-semibold text-gray-900 dark:text-white">
              <ng-icon name="heroBeaker" class="size-4 shrink-0 text-gray-400" aria-hidden="true" />
              Test drive
            </h3>
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">
              @if (reviewVersion(); as version) {
                Runs version {{ version }} — the snapshot you are deciding on.
              } @else {
                Runs this agent's current configuration.
              }
            </p>
          </div>
          <div class="flex shrink-0 items-center gap-1">
            @if (chat.hasMessages()) {
              <button
                type="button"
                (click)="clear()"
                class="rounded-md px-2 py-1 text-xs/5 font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              >
                Clear
              </button>
            }
            <!-- Widens the panel across the page rather than opening an overlay. The
                 conversation has to survive the toggle, so the element never moves in the
                 DOM — only its classes change — and a plain layout change needs none of
                 the focus trapping an overlay would. -->
            <button
              type="button"
              (click)="expandedChange.emit(!expanded())"
              [attr.aria-expanded]="expanded()"
              [attr.aria-label]="expanded() ? 'Collapse the test drive' : 'Expand the test drive to full width'"
              [title]="expanded() ? 'Collapse' : 'Expand to full width'"
              class="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <ng-icon
                [name]="expanded() ? 'heroArrowsPointingIn' : 'heroArrowsPointingOut'"
                class="size-4"
                aria-hidden="true"
              />
            </button>
          </div>
        </div>

        <!-- Never collapsed away. A reviewer who reads a clean test drive as "this works
             for everyone" has drawn the one wrong conclusion this panel can produce. -->
        <p
          class="mt-2 flex items-start gap-1.5 rounded-lg border border-state-warning-200 bg-state-warning-50 px-2.5 py-1.5 text-xs/5 text-state-warning-800 dark:border-state-warning-800/50 dark:bg-state-warning-900/20 dark:text-state-warning-300"
        >
          <ng-icon
            name="heroExclamationTriangle"
            class="mt-0.5 size-4 shrink-0"
            aria-hidden="true"
          />
          <span>
            Runs with <span class="font-medium">your</span> permissions. A bound tool you
            can reach may still be blocked for the people who pin this. Nothing here is
            saved to the author's conversations.
          </span>
        </p>
      </div>

      <div class="relative flex min-h-0 flex-1 flex-col">
        @if (!chat.hasMessages()) {
          <div class="flex flex-1 flex-col items-center justify-center gap-1 overflow-y-auto p-6 text-center">
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
              Ask {{ name() }} something
            </p>
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">
              The questions its users will ask are the ones worth trying.
            </p>
          </div>
          <div class="shrink-0 border-t border-gray-200 p-4 dark:border-gray-700">
            <!-- No @-mention: this panel exists to exercise one specific submission, and
                 handing its turn to another agent would make it lie. -->
            <app-chat-input
              [sessionId]="chat.sessionId()"
              [isChatLoading]="chat.isLoading()"
              [showFileControls]="false"
              [showVoiceControl]="false"
              [showSettingsControl]="false"
              [autoFocus]="false"
              [showAgentMentions]="false"
              (messageSubmitted)="onMessageSubmitted($event)"
              (messageCancelled)="onMessageCancelled()"
            />
          </div>
        } @else {
          <app-chat-container
            class="h-full"
            [messages]="chat.messages()"
            [sessionId]="chat.sessionId()"
            [assistant]="null"
            [isChatLoading]="chat.isLoading()"
            [streamingMessageId]="chat.streamingMessageId()"
            [greetingMessage]="greeting()"
            [config]="chatConfig"
            (messageSubmitted)="onMessageSubmitted($event)"
            (messageCancelled)="onMessageCancelled()"
          />
        }
      </div>

      @if (chat.error(); as message) {
        <p
          role="alert"
          class="shrink-0 border-t border-gray-200 px-4 py-2 text-xs/5 text-state-danger-700 dark:border-gray-700 dark:text-state-danger-400"
        >
          {{ message }}
        </p>
      }
    </div>
  `,
  styles: [':host { display: block; }'],
})
export class ReviewTestDriveComponent {
  protected readonly chat = inject(PreviewChatService);

  readonly agentId = input.required<string>();
  readonly name = input<string>('this agent');
  /** Which snapshot the server will run; absent when none backs the submission. */
  readonly reviewVersion = input<number | undefined>(undefined);
  /**
   * Whether the panel currently spans the page.
   *
   * Owned by the page, not by this component: expanding changes the page's grid, and the
   * panel is not in a position to know that. This only renders the control and reports the
   * intent.
   */
  readonly expanded = input(false);
  readonly expandedChange = output<boolean>();

  /**
   * `includeSystemPrompt` / `includeEnabledTools` are off for the same reason the agent
   * designer's preview turns them off: an agent resolves instructions, model, tools,
   * skills and memory server-side from its own record. Sending the reviewer's tool set
   * would fight the bindings and test something nobody will ever run.
   */
  private readonly opts = {
    includeSystemPrompt: false,
    includeEnabledTools: false,
    reviewPreview: true,
  };

  protected readonly chatConfig: Partial<ChatContainerConfig> = {
    embeddedMode: true,
    fullPageMode: false,
    showTopnav: false,
    showEmptyState: false,
    allowCloseAssistant: false,
    showFileControls: false,
    showVoiceControl: false,
    showSettingsControl: false,
  };

  constructor() {
    // A fresh session per agent, so moving between two queue rows never carries one
    // submission's exchange into the other's transcript.
    effect(() => {
      if (this.agentId()) this.chat.reset();
    });
  }

  protected greeting(): string {
    return `Test drive ${this.name()}`;
  }

  protected onMessageSubmitted(event: { content: string; timestamp: Date }): void {
    if (!event.content.trim()) return;
    void this.chat.sendMessage(event.content, this.agentId(), undefined, undefined, this.opts);
  }

  protected onMessageCancelled(): void {
    this.chat.cancelRequest();
  }

  protected clear(): void {
    this.chat.clearMessages();
  }
}
