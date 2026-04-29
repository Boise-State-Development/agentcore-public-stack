import { Component, computed, input, signal, effect, OnDestroy, inject, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { Message } from '../../services/models/message.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { MessageMetadataBadgesComponent } from './components/message-metadata-badges.component';
import { MessageActionsComponent } from './components/message-actions.component';
import { CitationDisplayComponent } from '../citation-display/citation-display.component';
import { PulsatingLoaderComponent } from '../../../components/pulsating-loader.component';
import { OAuthConsentPromptComponent } from './components/oauth-consent-prompt/oauth-consent-prompt.component';
import { ToolApprovalPromptComponent } from './components/tool-approval-prompt/tool-approval-prompt.component';
import {
  OAuthConsentRequest,
  OAuthConsentService,
} from '../../../services/oauth-consent/oauth-consent.service';
import {
  ToolApprovalRequest,
  ToolApprovalService,
} from '../../../services/tool-approval/tool-approval.service';

@Component({
  selector: 'app-message-list',
  imports: [
    UserMessageComponent,
    AssistantMessageComponent,
    MessageActionsComponent,
    MessageMetadataBadgesComponent,
    CitationDisplayComponent,
    PulsatingLoaderComponent,
    OAuthConsentPromptComponent,
    ToolApprovalPromptComponent,
  ],
  templateUrl: './message-list.component.html',
  styleUrl: './message-list.component.css',
})
export class MessageListComponent implements OnDestroy {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);

  // Constants for scroll behavior and layout
  private readonly HEADER_HEIGHT = 64;
  private readonly SCROLL_PADDING = 16;
  private readonly RESIZE_DEBOUNCE_MS = 150;

  messages = input.required<Message[]>();
  isChatLoading = input<boolean>(false);
  streamingMessageId = input<string | null>(null);
  embeddedMode = input<boolean>(false);

  private consentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);

  /** Pending consent prompts whose anchor message id isn't in the loaded
   *  message list — typically the case when an interrupt fires on a turn
   *  whose partial assistant message wasn't persisted to AgentCore Memory.
   *  Rendered at the end of the conversation so the user still sees the
   *  affordance instead of a silently stalled tool call. */
  protected unanchoredInterrupts = computed<OAuthConsentRequest[]>(() => {
    const ids = new Set(this.messages().map((m) => m.id));
    return this.consentService.pending().filter((req) => !req.messageId || !ids.has(req.messageId));
  });

  /** Pending tool-approval prompts, rendered at the end of the conversation.
   *  Sourced from both live `tool_approval_required` SSE events during a
   *  turn and the `PendingInterrupt(kind="tool_approval")` rows that
   *  `MessageMapService.hydratePendingInterrupts` replays on session load,
   *  so a mid-prompt refresh rehydrates the prompt rather than orphaning
   *  it. We don't anchor next to the triggering assistant message (the way
   *  OAuth prompts do) because the approval is for the *next* tool call,
   *  not the assistant text that just streamed. */
  protected pendingToolApprovals = computed<ToolApprovalRequest[]>(() =>
    this.toolApprovalService.pending(),
  );

  // Calculate the spacer height dynamically
  // This creates space at the bottom so user messages can scroll to the top
  spacerHeight = signal(0);

  // Store debounced resize listener for cleanup
  private resizeListener = this.debounce(
    () => this.calculateSpacerHeight(),
    this.RESIZE_DEBOUNCE_MS
  );

  constructor() {
    if (this.isBrowser) {
      // Only recalculate when message count changes, not on every message update
      effect(() => {
        this.messages().length;
        this.calculateSpacerHeight();
      });

      // Add resize listener
      window.addEventListener('resize', this.resizeListener);
    }
  }

  ngOnDestroy() {
    if (this.isBrowser) {
      window.removeEventListener('resize', this.resizeListener);
    }
  }

  /**
   * Calculates the height needed for the bottom spacer
   * This ensures there's enough space for user messages to scroll to the top
   */
  private calculateSpacerHeight(): void {
    if (!this.isBrowser) return;

    // Wait for next frame to ensure DOM is updated
    requestAnimationFrame(() => {
      const viewportHeight = window.innerHeight;
      const spacerHeight = viewportHeight - this.HEADER_HEIGHT;
      this.spacerHeight.set(spacerHeight);
    });
  }

  /**
   * Scrolls to a specific message by ID
   * Call this explicitly when user submits a message
   * Works in both full-page mode (window scroll) and embedded mode (container scroll)
   */
  scrollToMessage(messageId: string): void {
    if (!this.isBrowser) return;

    const element = document.getElementById(`message-${messageId}`);
    if (!element) return;

    if (this.embeddedMode()) {
      // In embedded mode, use scrollIntoView which works with any scroll container
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      // In full-page mode, use window scroll with offset for fixed header
      const elementRect = element.getBoundingClientRect();
      const absoluteElementTop = elementRect.top + window.scrollY;
      const offset = this.HEADER_HEIGHT + this.SCROLL_PADDING;

      window.scrollTo({
        top: absoluteElementTop - offset,
        behavior: 'smooth'
      });
    }
  }

  /**
   * Scrolls to the last user message
   */
  scrollToLastUserMessage(): void {
    const msgs = this.messages();
    const lastUserMsg = [...msgs].reverse().find(m => m.role === 'user');
    if (lastUserMsg) {
      this.scrollToMessage(lastUserMsg.id);
    }
  }

  /**
   * Debounces a function to limit how often it can be called
   */
  private debounce<T extends (...args: any[]) => any>(
    fn: T,
    delay: number
  ): (...args: Parameters<T>) => void {
    let timeoutId: ReturnType<typeof setTimeout>;
    return (...args: Parameters<T>) => {
      clearTimeout(timeoutId);
      timeoutId = setTimeout(() => fn(...args), delay);
    };
  }
}

