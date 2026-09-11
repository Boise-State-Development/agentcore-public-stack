import {
  Component,
  ChangeDetectionStrategy,
  OnDestroy,
  input,
  output,
  computed,
  inject,
  effect,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroSparkles,
  heroArrowTopRightOnSquare,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { ChatContainerComponent, ChatContainerConfig } from '../../../session/components/chat-container/chat-container.component';
import { ChatInputComponent } from '../../../session/components/chat-input/chat-input.component';
import { PreviewSessionService } from '../../../shared/preview/preview-session.service';
import {
  AgentLaunchCardComponent,
  AgentLaunchCardView,
} from '../../components/agent-launch-card.component';
import { ModelService } from '../../../session/services/model/model.service';

/**
 * Live preview for the Agent Designer, side-by-side with the editor.
 *
 * Streams through the SAME real invocation path the main chat uses
 * (`POST /chat/stream` with a `preview-` session id the backend skips
 * persisting), scoped to the agent by id. Because `agentId == assistantId`,
 * the harness resolves the agent's FULL set from the SAVED record server-side
 * — instructions + model + params + tools + skills + memory — so the preview
 * exercises the agent exactly as a real invoker would. The request body carries
 * no `system_prompt` and no tool selection of its own — either would fight the
 * bindings, and a long persona sent as `system_prompt` blows the length cap
 * (422). The visible consequence is that the preview runs what is SAVED, not
 * what is typed, so a dirty form shows a "save to apply" banner making that gap
 * explicit rather than letting the pane quietly answer as the wrong agent.
 *
 * It deliberately does NOT restate the model, tools, skills or memory spaces. That was
 * a capability strip here, and it was a read-out of the form sitting one column to the
 * left: the same facts, in a second place that could only ever agree or be wrong.
 *
 * Runs on the SAME services as the main chat — `ChatRequestService`,
 * `ChatHttpService`, `StreamParserService`, `MessageMapService` — via a
 * component-scoped `PreviewSessionService` that owns this pane's `preview-`
 * session id. It used to run on a parallel `PreviewChatService` that
 * re-implemented the SSE consumer and silently dropped every event it hadn't
 * implemented, including `tool_approval_required` and `oauth_required` — so an
 * approval-gated tool call was never surfaced and therefore never dispatched.
 * Isolation from the main session page comes from the session key the whole
 * chat stack is already built on, not from a second implementation.
 */
@Component({
  selector: 'app-agent-preview',
  standalone: true,
  imports: [NgIcon, ChatContainerComponent, ChatInputComponent, AgentLaunchCardComponent],
  providers: [
    PreviewSessionService,
    provideIcons({
      heroSparkles,
      heroArrowTopRightOnSquare,
      heroExclamationTriangle,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (agentId()) {
      <div class="flex h-full flex-col overflow-hidden bg-gray-50 dark:bg-gray-900">
        <!-- Header: title + open-in-full -->
        <div class="shrink-0 border-b border-gray-200/80 bg-gray-50 px-4 py-3 dark:border-gray-700/60 dark:bg-gray-900">
          <div class="flex items-center justify-between gap-2">
            <div class="min-w-0">
              <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Preview</h3>
              <p class="text-xs/5 text-gray-500 dark:text-gray-400">Chat with this agent — its real tools, skills, and memory.</p>
            </div>
            <div class="flex shrink-0 items-center gap-1">
              @if (hasMessages()) {
                <button type="button" (click)="clearChat()" class="rounded-md px-2 py-1 text-xs/5 font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200">
                  Clear
                </button>
              }
              <button type="button" (click)="openFull.emit()" class="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-primary-600 dark:hover:bg-gray-700" aria-label="Open in full chat" title="Open in full chat">
                <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
              </button>
            </div>
          </div>

          <!-- Dirty banner: bindings/model/params resolve from the saved record -->
          @if (isDirty()) {
            <div class="mt-2 flex items-center gap-2 rounded-lg border border-state-warning-200 bg-state-warning-50 px-2.5 py-1.5 dark:border-state-warning-800/50 dark:bg-state-warning-900/20">
              <ng-icon name="heroExclamationTriangle" class="size-4 shrink-0 text-state-warning-600 dark:text-state-warning-400" aria-hidden="true" />
              <p class="min-w-0 flex-1 text-xs/5 text-state-warning-800 dark:text-state-warning-300">The preview runs the saved agent. Save to apply your latest changes.</p>
              @if (canSave()) {
                <button type="button" (click)="save.emit()" [disabled]="saving()" class="shrink-0 rounded-md bg-state-warning-600 px-2 py-1 text-xs/5 font-semibold text-white transition hover:bg-state-warning-700 disabled:opacity-50">
                  {{ saving() ? 'Saving…' : 'Save' }}
                </button>
              }
            </div>
          }
        </div>

        <!-- Chat surface -->
        <div class="relative flex min-h-0 flex-1 flex-col">
          @if (!hasMessages()) {
            <div class="flex flex-1 items-center justify-center overflow-y-auto bg-gray-50 p-6 dark:bg-gray-900">
              <app-agent-launch-card
                [view]="cardView()"
                (starterSelected)="onStarterSelected($event)"
              />
            </div>
            <div class="shrink-0 bg-gray-50 px-4 pb-4 pt-2 dark:bg-gray-900">
              <!-- No @-mention here (D11): the preview already runs the thing being
                   edited, so handing its turn to another Agent would make it lie. -->
              <app-chat-input
                [sessionId]="preview.sessionId()"
                [isChatLoading]="preview.isLoading()"
                [showFileControls]="true"
                [showVoiceControl]="false"
                [showSettingsControl]="false"
                [autoFocus]="false"
                [showAgentMentions]="false"
                [showAnnouncements]="false"
                (messageSubmitted)="onMessageSubmitted($event)"
                (messageCancelled)="onMessageCancelled()"
              />
            </div>
          } @else {
            <app-chat-container
              class="h-full"
              [messages]="preview.messages()"
              [sessionId]="preview.sessionId()"
              [assistant]="null"
              [isChatLoading]="preview.isLoading()"
              [streamingMessageId]="preview.streamingMessageId()"
              [greetingMessage]="greetingMessage()"
              [config]="chatConfigMessagesOnly"
              (messageSubmitted)="onMessageSubmitted($event)"
              (messageCancelled)="onMessageCancelled()"
            />
          }
        </div>
      </div>
    } @else {
      <!-- No saved agent yet -->
      <div class="flex h-full items-center justify-center rounded-2xl border border-dashed border-gray-300 bg-gray-50 dark:border-gray-600 dark:bg-gray-900/50">
        <div class="px-6 py-12 text-center">
          <ng-icon name="heroSparkles" class="mx-auto size-10 text-gray-400 dark:text-gray-500" aria-hidden="true" />
          <h3 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">Preview your agent</h3>
          <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">Save this agent to start a live preview here.</p>
          @if (canSave()) {
            <button type="button" (click)="save.emit()" [disabled]="saving()" class="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-primary-500 px-3 py-2 text-sm/6 font-semibold text-white shadow-xs transition hover:bg-primary-600 disabled:opacity-50">
              {{ saving() ? 'Saving…' : 'Save & preview' }}
            </button>
          }
        </div>
      </div>
    }
  `,
  styles: [':host { display: block; height: 100%; }'],
})
export class AgentPreviewComponent implements OnDestroy {
  readonly preview = inject(PreviewSessionService);
  private readonly modelService = inject(ModelService);

  // Persona (live from the form)
  readonly agentId = input<string | null>(null);
  readonly name = input<string>('');
  readonly description = input<string>('');
  readonly emoji = input<string>('');
  readonly starters = input<string[]>([]);

  // Capability strip (current selections — reflected once saved)
  readonly modelId = input<string | null>(null);

  // Save awareness
  readonly isDirty = input<boolean>(false);
  readonly saving = input<boolean>(false);
  readonly canSave = input<boolean>(true);

  readonly save = output<void>();
  readonly openFull = output<void>();

  readonly hasMessages = this.preview.hasMessages;

  readonly greetingMessage = computed(() =>
    this.name() ? `Chat with ${this.name()}` : 'Start a conversation',
  );

  /**
   * The launch card as the author's own agent sees it — built from the live form rather
   * than a fetched record, so a name typed a second ago is already on the tile.
   *
   * `listed: false` regardless of the real listing state: the store affordances are Add
   * and Agent details, and neither means anything on the page where you are editing the
   * thing. Capabilities are likewise omitted — the header's capability strip already
   * names what this agent runs with, from the live selections rather than the saved ones.
   */
  readonly cardView = computed<AgentLaunchCardView>(() => ({
    agentId: this.agentId() ?? '',
    name: this.name(),
    description: this.description(),
    emoji: this.emoji(),
    starters: this.starters(),
    listed: false,
  }));

  readonly chatConfigMessagesOnly: Partial<ChatContainerConfig> = {
    embeddedMode: true,
    fullPageMode: false,
    showTopnav: false,
    showEmptyState: false,
    allowCloseAssistant: false,
    showFileControls: true,
    showVoiceControl: false,
    showSettingsControl: false,
  };

  constructor() {
    // Fresh preview session whenever the previewed agent changes.
    effect(() => {
      if (this.agentId()) this.preview.reset();
    });

    // Pin the chat-input model picker to the agent's model — the same lock the
    // main session page applies for a real agent conversation. Without it the
    // preview's picker shows the user's global model and lets them switch it,
    // which is a lie: the harness resolves the model from the agent's binding
    // server-side regardless. The lock lives in the root ModelService (shared
    // with the main chat), so we release it on destroy; navigating into a plain
    // chat also clears it idempotently via the session page's self-heal effect.
    effect(() => {
      const modelId = this.modelId();
      if (modelId) {
        this.modelService.lockToAgentModel(modelId);
      } else {
        this.modelService.clearAgentModelLock();
      }
    });
  }

  ngOnDestroy(): void {
    this.modelService.clearAgentModelLock();
  }

  onMessageSubmitted(event: { content: string; timestamp: Date; fileUploadIds?: string[] }): void {
    this.send(event.content, event.fileUploadIds);
  }

  onMessageCancelled(): void {
    this.preview.cancel();
  }

  /** Clear also starts a fresh preview session — see `PreviewSessionService.reset`. */
  clearChat(): void {
    this.preview.reset();
  }

  onStarterSelected(starter: string): void {
    this.send(starter);
  }

  /**
   * Errors are already surfaced by the shared stack: `ChatHttpService` raises a
   * toast via `ErrorService` and tears the stream down, and a conversational
   * error arrives as a `stream_error` the message list renders. Rethrowing from
   * a template event handler would only reach Angular's global error handler.
   */
  private send(message: string, fileUploadIds?: string[]): void {
    const id = this.agentId();
    if (!id || !message.trim()) return;
    void this.preview.send(id, message, { fileUploadIds }).catch(() => {});
  }
}
