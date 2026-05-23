import {
  Component,
  ChangeDetectionStrategy,
  input,
  computed,
  inject,
  effect,
} from '@angular/core';
import { ChatContainerComponent, ChatContainerConfig } from '../../../session/components/chat-container/chat-container.component';
import { ChatInputComponent } from '../../../session/components/chat-input/chat-input.component';
import { PreviewChatService } from '../services/preview-chat.service';
import { Assistant } from '../../models/assistant.model';
import { AssistantCardComponent } from '../../components/assistant-card.component';

/**
 * Component that wraps ChatContainerComponent for assistant preview functionality.
 *
 * Provides PreviewChatService at the component level to ensure complete isolation
 * from the main session page. This prevents state collision when both the main
 * chat and preview are active simultaneously.
 *
 * Note: We intentionally don't use StreamParserService here because it has dependencies
 * on singleton services (ChatStateService, ErrorService, QuotaWarningService) that would
 * cause state pollution. Instead, PreviewChatService handles SSE parsing inline with
 * simplified logic sufficient for testing assistant instructions.
 */
@Component({
  selector: 'app-assistant-preview',
  standalone: true,
  imports: [ChatContainerComponent, AssistantCardComponent, ChatInputComponent],
  providers: [PreviewChatService], // Component-scoped: manages preview-specific state
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (assistantId()) {
      <div class="h-full flex flex-col bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
        <!-- Header (fixed height) -->
        <div class="shrink-0 flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
          <div>
            <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Preview Chat</h3>
            <p class="text-xs text-gray-500 dark:text-gray-400">Test your assistant's responses</p>
          </div>
          @if (hasMessages()) {
            <button
              type="button"
              (click)="clearChat()"
              class="text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            >
              Clear chat
            </button>
          }
        </div>

        <!-- Chat Container (fills remaining space) -->
        <div class="flex-1 min-h-0 relative flex flex-col">
          @if (!hasMessages()) {
            <!-- Show assistant card when no messages -->
            <div class="flex-1 flex items-center justify-center p-6 overflow-y-auto bg-white dark:bg-gray-800">
              <app-assistant-card
                [name]="name()"
                [description]="description()"
                [emoji]="emoji()"
                [starters]="starters()"
                (starterSelected)="onStarterSelected($event)"
              />
            </div>
            <!-- Chat input at bottom when showing card -->
            <div class="shrink-0 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
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
            <!-- Chat container handles both messages and input in embedded mode -->
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
      <!-- Placeholder when no assistant ID -->
      <div class="flex h-full items-center justify-center rounded-lg border border-dashed border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-900/50">
        <div class="text-center px-6 py-12">
          <svg class="mx-auto size-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
          </svg>
          <h3 class="mt-4 text-sm font-semibold text-gray-900 dark:text-gray-100">No Preview Available</h3>
          <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
            Save your assistant to enable preview chat
          </p>
        </div>
      </div>
    }
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
    }
  `],
})
export class AssistantPreviewComponent {
  // Inject the component-scoped PreviewChatService
  readonly previewChatService = inject(PreviewChatService);

  // Inputs from parent form
  readonly assistantId = input<string | null>(null);
  readonly name = input<string>('');
  readonly description = input<string>('');
  readonly instructions = input<string>('');
  readonly emoji = input<string>('');
  readonly starters = input<string[]>([]);

  // Chat container configuration for embedded mode
  readonly chatConfig: Partial<ChatContainerConfig> = {
    embeddedMode: true,
    fullPageMode: false,
    showTopnav: false,
    showEmptyState: true,
    allowCloseAssistant: false, // Don't allow closing in preview
    showFileControls: true,
    showVoiceControl: false, // Voice mode is not meaningful in the editor preview
    showSettingsControl: false, // Settings panel isn't wired into the preview
  };

  // Chat container configuration for messages-only mode (no input, used when we render input separately)
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

  // Computed: build an Assistant-like object from form inputs
  readonly builtAssistant = computed<Assistant | null>(() => {
    const id = this.assistantId();
    const name = this.name();
    if (!id || !name) return null;

    return {
      assistantId: id,
      ownerId: '',
      ownerName: '',
      name: name,
      description: this.description(),
      instructions: this.instructions(),
      vectorIndexId: '',
      visibility: 'PRIVATE',
      tags: [],
      starters: this.starters(),
      usageCount: 0,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      status: 'DRAFT',
    } as Assistant;
  });

  // Computed: custom greeting message
  readonly greetingMessage = computed(() => {
    const name = this.name();
    if (name) {
      return `Chat with ${name}`;
    }
    return 'Start a conversation';
  });

  // Computed: check if there are messages
  readonly hasMessages = this.previewChatService.hasMessages;

  // Reset preview when assistant ID changes
  constructor() {
    effect(() => {
      const id = this.assistantId();
      // Reset the preview chat when assistant ID changes
      // This ensures fresh state when switching between assistants
      if (id) {
        this.previewChatService.reset();
      }
    });
  }

  /**
   * Handle message submission from chat input
   */
  onMessageSubmitted(event: { content: string; timestamp: Date; fileUploadIds?: string[] }): void {
    const assistantId = this.assistantId();
    if (!assistantId || !event.content.trim()) {
      return;
    }

    this.previewChatService.sendMessage(
      event.content,
      assistantId,
      this.instructions(),
      event.fileUploadIds,
    );
  }

  /**
   * Handle message cancellation
   */
  onMessageCancelled(): void {
    this.previewChatService.cancelRequest();
  }

  /**
   * Clear the preview chat
   */
  clearChat(): void {
    this.previewChatService.clearMessages();
  }

  /**
   * Handle starter selection from assistant card
   */
  onStarterSelected(starter: string): void {
    const assistantId = this.assistantId();
    if (!assistantId || !starter.trim()) {
      return;
    }
    this.previewChatService.sendMessage(starter, assistantId, this.instructions());
  }
}
