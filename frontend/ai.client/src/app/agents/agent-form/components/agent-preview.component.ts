import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
  computed,
  inject,
  effect,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCpuChip,
  heroWrenchScrewdriver,
  heroSparkles,
  heroCircleStack,
  heroArrowTopRightOnSquare,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { ChatContainerComponent, ChatContainerConfig } from '../../../session/components/chat-container/chat-container.component';
import { ChatInputComponent } from '../../../session/components/chat-input/chat-input.component';
import { PreviewChatService } from '../../../assistants/assistant-form/services/preview-chat.service';
import { AssistantCardComponent } from '../../../assistants/components/assistant-card.component';

/**
 * Live preview for the Agent Designer, side-by-side with the editor.
 *
 * Streams through the SAME real invocation path the main chat uses
 * (`POST /chat/stream` with a `preview-` session id the backend skips
 * persisting), scoped to the agent by id. Because `agentId == assistantId`,
 * the harness resolves the agent's FULL set from the SAVED record server-side
 * — instructions + model + params + tools + skills + memory — so the preview
 * exercises the agent exactly as a real invoker would. Unlike the assistant
 * preview it sends a minimal body (no `system_prompt`/owner-tools override,
 * which would fight the bindings and blow the prompt cap for a long persona),
 * so a dirty form shows a "save to apply" banner and a capability strip makes
 * the resolved context explicit — the two things the assistant preview lacked.
 *
 * Reuses the assistant preview's `PreviewChatService` (opting out of its
 * system_prompt + owner-tools injection) and provides it at the component
 * level so its state stays isolated from the main session page.
 */
@Component({
  selector: 'app-agent-preview',
  standalone: true,
  imports: [NgIcon, ChatContainerComponent, ChatInputComponent, AssistantCardComponent],
  providers: [
    PreviewChatService,
    provideIcons({
      heroCpuChip,
      heroWrenchScrewdriver,
      heroSparkles,
      heroCircleStack,
      heroArrowTopRightOnSquare,
      heroExclamationTriangle,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (agentId()) {
      <div class="flex h-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
        <!-- Header: title + capability strip + open-in-full -->
        <div class="shrink-0 border-b border-gray-200 bg-white px-4 py-3 dark:border-gray-700 dark:bg-gray-800">
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

          <!-- Capability strip: what the agent runs with -->
          <div class="mt-2 flex flex-wrap items-center gap-1.5">
            @if (modelLabel()) {
              <span class="inline-flex items-center gap-1 rounded-full bg-primary-50 px-2 py-0.5 text-xs/5 font-medium text-primary-700 dark:bg-primary-500/10 dark:text-primary-300">
                <ng-icon name="heroCpuChip" class="size-3.5" aria-hidden="true" />
                {{ modelLabel() }}
              </span>
            }
            @if (toolCount() > 0) {
              <span class="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-xs/5 font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                <ng-icon name="heroWrenchScrewdriver" class="size-3.5" aria-hidden="true" />
                {{ toolCount() }} {{ toolCount() === 1 ? 'tool' : 'tools' }}
              </span>
            }
            @if (skillCount() > 0) {
              <span class="inline-flex items-center gap-1 rounded-full bg-purple-50 px-2 py-0.5 text-xs/5 font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                <ng-icon name="heroSparkles" class="size-3.5" aria-hidden="true" />
                {{ skillCount() }} {{ skillCount() === 1 ? 'skill' : 'skills' }}
              </span>
            }
            @if (memoryCount() > 0) {
              <span class="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs/5 font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
                <ng-icon name="heroCircleStack" class="size-3.5" aria-hidden="true" />
                {{ memoryCount() }} {{ memoryCount() === 1 ? 'space' : 'spaces' }}
              </span>
            }
          </div>

          <!-- Dirty banner: bindings/model/params resolve from the saved record -->
          @if (isDirty()) {
            <div class="mt-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 dark:border-amber-800/50 dark:bg-amber-900/20">
              <ng-icon name="heroExclamationTriangle" class="size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden="true" />
              <p class="min-w-0 flex-1 text-xs/5 text-amber-800 dark:text-amber-300">The preview runs the saved agent. Save to apply your latest changes.</p>
              @if (canSave()) {
                <button type="button" (click)="save.emit()" [disabled]="saving()" class="shrink-0 rounded-md bg-amber-600 px-2 py-1 text-xs/5 font-semibold text-white transition hover:bg-amber-700 disabled:opacity-50">
                  {{ saving() ? 'Saving…' : 'Save' }}
                </button>
              }
            </div>
          }
        </div>

        <!-- Chat surface -->
        <div class="relative flex min-h-0 flex-1 flex-col">
          @if (!hasMessages()) {
            <div class="flex flex-1 items-center justify-center overflow-y-auto bg-white p-6 dark:bg-gray-800">
              <app-assistant-card
                [name]="name()"
                [description]="description()"
                [emoji]="emoji()"
                [starters]="starters()"
                (starterSelected)="onStarterSelected($event)"
              />
            </div>
            <div class="shrink-0 border-t border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <app-chat-input
                [sessionId]="previewChatService.sessionId()"
                [isChatLoading]="previewChatService.isLoading()"
                [showFileControls]="true"
                [showVoiceControl]="false"
                [showSettingsControl]="false"
                [autoFocus]="false"
                (messageSubmitted)="onMessageSubmitted($event)"
                (messageCancelled)="onMessageCancelled()"
              />
            </div>
          } @else {
            <app-chat-container
              class="h-full"
              [messages]="previewChatService.messages()"
              [sessionId]="previewChatService.sessionId()"
              [assistant]="null"
              [isChatLoading]="previewChatService.isLoading()"
              [streamingMessageId]="previewChatService.streamingMessageId()"
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
export class AgentPreviewComponent {
  readonly previewChatService = inject(PreviewChatService);

  // Persona (live from the form)
  readonly agentId = input<string | null>(null);
  readonly name = input<string>('');
  readonly description = input<string>('');
  readonly emoji = input<string>('');
  readonly starters = input<string[]>([]);

  // Capability strip (current selections — reflected once saved)
  readonly modelLabel = input<string | null>(null);
  readonly toolCount = input<number>(0);
  readonly skillCount = input<number>(0);
  readonly memoryCount = input<number>(0);

  // Save awareness
  readonly isDirty = input<boolean>(false);
  readonly saving = input<boolean>(false);
  readonly canSave = input<boolean>(true);

  readonly save = output<void>();
  readonly openFull = output<void>();

  readonly hasMessages = this.previewChatService.hasMessages;

  readonly greetingMessage = computed(() =>
    this.name() ? `Chat with ${this.name()}` : 'Start a conversation',
  );

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
      if (this.agentId()) this.previewChatService.reset();
    });
  }

  /** Agents resolve instructions + model + tools + skills + memory server-side from
   * the saved record — so the preview sends a minimal body and opts out of the
   * assistant preview's live-instructions and owner-tools injection. */
  private readonly agentPreviewOpts = { includeSystemPrompt: false, includeEnabledTools: false };

  onMessageSubmitted(event: { content: string; timestamp: Date; fileUploadIds?: string[] }): void {
    const id = this.agentId();
    if (!id || !event.content.trim()) return;
    this.previewChatService.sendMessage(event.content, id, undefined, event.fileUploadIds, this.agentPreviewOpts);
  }

  onMessageCancelled(): void {
    this.previewChatService.cancelRequest();
  }

  clearChat(): void {
    this.previewChatService.clearMessages();
  }

  onStarterSelected(starter: string): void {
    const id = this.agentId();
    if (!id || !starter.trim()) return;
    this.previewChatService.sendMessage(starter, id, undefined, undefined, this.agentPreviewOpts);
  }
}
