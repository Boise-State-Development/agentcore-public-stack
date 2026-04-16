import { Component, inject, effect, Signal, signal, computed, OnDestroy } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { v4 as uuidv4 } from 'uuid';
import { ChatRequestService } from './services/chat/chat-request.service';
import { MessageMapService } from './services/session/message-map.service';
import { Message } from './services/models/message.model';
import { SessionService } from './services/session/session.service';
import { ChatStateService } from './services/chat/chat-state.service';
import { SidenavService } from '../services/sidenav/sidenav.service';
import { HeaderService } from '../services/header/header.service';
import { ModelService } from './services/model/model.service';
import { ModelSettings } from '../components/model-settings/model-settings';
import { UserService } from '../auth/user.service';
import { ChatHttpService } from './services/chat/chat-http.service';
import { StreamParserService } from './services/chat/stream-parser.service';
import { Dialog } from '@angular/cdk/dialog';
import { AssistantService } from '../assistants/services/assistant.service';
import { Assistant } from '../assistants/models/assistant.model';
import { ChatContainerComponent, ChatContainerConfig } from './components/chat-container/chat-container.component';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../assistants/components/share-assistant-dialog.component';
import { VoiceChatService } from './services/voice';

@Component({
  selector: 'app-session-page',
  imports: [ChatContainerComponent, ModelSettings],
  templateUrl: './session.page.html',
  styleUrl: './session.page.css',
})
export class ConversationPage implements OnDestroy {
  private route = inject(ActivatedRoute);
  private sessionService = inject(SessionService);
  private chatRequestService = inject(ChatRequestService);
  private messageMapService = inject(MessageMapService);
  private chatStateService = inject(ChatStateService);
  protected sidenavService = inject(SidenavService);
  private headerService = inject(HeaderService);
  private modelService = inject(ModelService);
  private userService = inject(UserService);
  private chatHttpService = inject(ChatHttpService);
  private streamParserService = inject(StreamParserService);
  private assistantService = inject(AssistantService);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private voiceChatService = inject(VoiceChatService);

  sessionId = signal<string | null>(null);
  assistantIdFromQuery = signal<string | null>(null);

  assistant = signal<Assistant | null>(null);
  assistantError = signal<string | null>(null);
  isLoadingAssistant = signal(false);
  isSettingsOpen = signal(false);

  /**
   * Staged session ID for file uploads before the first message is sent.
   * This allows users to attach files before typing their first message.
   * The staged session ID is used for file uploads and then consumed when
   * the first message is submitted.
   */
  private stagedSessionId = signal<string | null>(null);

  /**
   * Effective session ID to pass to chat-input for file uploads.
   * Returns the route sessionId if navigating to an existing session,
   * or creates/returns a staged session ID for new conversations.
   */
  readonly effectiveSessionId = computed(() => {
    return this.sessionId() ?? this.stagedSessionId();
  });

  // Writable signal that holds the current messages signal reference
  private messagesSignal = signal<Signal<Message[]>>(signal([]));

  // Computed that unwraps the current messages signal, merging in
  // real-time voice messages. Voice messages are cleared on voice close
  // after being persisted to the map, so there's no double-counting.
  readonly messages = computed(() => {
    const base = this.messagesSignal()();
    const voice = this.voiceChatService.voiceMessages();
    return voice.length > 0 ? [...base, ...voice] : base;
  });

  // Get user's first name from the user service
  private firstName = computed(() => {
    const user = this.userService.currentUser();
    return user?.firstName || null;
  });

  // Greeting message templates (use {name} as placeholder for first name)
  private greetingTemplates = [
    'How can I help you today, {name}?',
    'What would you like to know, {name}?',
    'Ready to assist you, {name}!',
    'What can I do for you, {name}?',
    "Let's get started, {name}!",
  ];

  // Fallback greetings when user name is not available
  private fallbackGreetings = [
    'How can I help you today?',
    'What would you like to know?',
    'Ready to assist you!',
    'What can I do for you?',
    "Let's get started!",
  ];

  // Store the selected template index for consistency
  private selectedGreetingIndex = Math.floor(Math.random() * this.greetingTemplates.length);

  // Computed greeting message that reacts to user changes
  greetingMessage = computed(() => {
    const name = this.firstName();
    if (name) {
      return this.greetingTemplates[this.selectedGreetingIndex].replace('{name}', name);
    }
    return this.fallbackGreetings[this.selectedGreetingIndex];
  });

  private routeSubscription?: Subscription;
  private queryParamSubscription?: Subscription;
  readonly sessionConversation = this.sessionService.currentSession;
  readonly isChatLoading = this.chatStateService.isChatLoading;
  readonly isLoadingSession = this.messageMapService.isLoadingSession;
  readonly streamingMessageId = this.streamParserService.streamingMessageId;

  // Computed signal to check if session has messages
  readonly hasMessages = computed(() => this.messages().length > 0);

  // Chat container configuration for full-page mode
  readonly chatConfig: Partial<ChatContainerConfig> = {
    fullPageMode: true,
    showTopnav: true,
    showEmptyState: true,
    allowCloseAssistant: true,
    showFileControls: true,
    embeddedMode: false,
  };

  // Computed signal to determine if assistant can be closed
  // Only allow closing if: no messages exist AND assistant is from query param (not session preferences)
  readonly canCloseAssistant = computed(() => {
    return !this.hasMessages() && !!this.assistantIdFromQuery() && !!this.assistant();
  });

  // Show skeleton when loading a session that matches current route and has no messages yet
  readonly showSkeleton = computed(() => {
    const loadingSessionId = this.isLoadingSession();
    const currentSessionId = this.sessionId();
    return loadingSessionId !== null && loadingSessionId === currentSessionId && !this.hasMessages();
  });

  constructor() {
    // Control header visibility based on whether there are messages
    effect(() => {
      if (this.hasMessages()) {
        this.headerService.showHeaderContent();
      } else {
        this.headerService.hideHeaderContent();
      }
    });

    // Apply model from session preferences when session metadata loads
    effect(() => {
      const session = this.sessionConversation();
      if (session?.preferences?.lastModel) {
        this.modelService.setSelectedModelById(session.preferences.lastModel);
      }
    });

    // Priority-based assistant loading: URL query param first, then session preferences
    effect(() => {
      const queryAssistantId = this.assistantIdFromQuery();
      const session = this.sessionConversation();
      const sessionAssistantId = session?.preferences?.assistantId;
      const currentSessionId = this.sessionId();
      
      // Priority 1: URL query parameter (highest priority)
      if (queryAssistantId) {
        // Validate: Can only attach to new sessions (no messages)
        if (currentSessionId && this.hasMessages()) {
          this.assistantError.set('Assistants can only be attached to new sessions');
          this.assistant.set(null);
          this.clearAssistantIdFromUrl();
          return;
        }
        // Load from query param (existence check only, no access validation)
        this.loadAssistant(queryAssistantId, false).catch(error => {
          console.error('Failed to load assistant from query param:', error);
        });
        return;
      }
      
      // Priority 2: Session preferences (fallback for existing sessions)
      if (sessionAssistantId && currentSessionId) {
        // Load from preferences - allow even if session has messages (persisted assistant)
        this.loadAssistant(sessionAssistantId, true).catch(error => {
          console.error('Failed to load assistant from session preferences:', error);
        });
        return;
      }
      
      // No assistant to load
      this.assistant.set(null);
      this.assistantError.set(null);
    });

    // Subscribe to route parameter changes
    this.routeSubscription = this.route.paramMap.subscribe(async params => {
      const id = params.get('sessionId');
      this.sessionId.set(id);
      if (id) {
        // Update the messages signal reference (this triggers reactivity)
        this.messagesSignal.set(this.messageMapService.getMessagesForSession(id));

        // Set loading state immediately before async call to show skeleton
        this.messageMapService.setLoadingSession(id);

        // Trigger fetching session metadata to populate currentSession
        this.sessionService.setSessionMetadataId(id);

        // Load messages from API for deep linking support
        try {
          await this.messageMapService.loadMessagesForSession(id);
        } catch (error) {
          console.error('Failed to load messages for session:', id, error);
        }
      } else {
        // No session selected, clear the session metadata
        this.sessionService.setSessionMetadataId(null);
      }
    });

    // Subscribe to query parameter changes for assistantId
    this.queryParamSubscription = this.route.queryParamMap.subscribe(params => {
      const assistantId = params.get('assistantId');
      this.assistantIdFromQuery.set(assistantId);
    });
  }

  ngOnDestroy() {
    this.routeSubscription?.unsubscribe();
    this.queryParamSubscription?.unsubscribe();
  }

  onMessageSubmitted(message: { content: string, timestamp: Date, fileUploadIds?: string[] }) {
    // Use the effective session ID (route sessionId or staged sessionId)
    const sessionIdToUse = this.effectiveSessionId();

    // Get assistantId from query param (priority 1) or session preferences (priority 2)
    const queryAssistantId = this.assistantIdFromQuery();
    const sessionAssistantId = this.sessionConversation()?.preferences?.assistantId;
    const assistantIdToUse = queryAssistantId || sessionAssistantId || undefined;

    // Set loading state before submitting
    this.chatStateService.setChatLoading(true);

    // Submit the chat request with file upload IDs and assistant ID if present
    this.chatRequestService.submitChatRequest(
      message.content,
      sessionIdToUse,
      message.fileUploadIds,
      assistantIdToUse
    ).catch((error) => {
      console.error('Error sending chat request:', error);
    });

    // Clear the staged session ID after submission (it's now a real session)
    if (this.stagedSessionId()) {
      this.stagedSessionId.set(null);
    }
  }

  /**
   * Called when user selects a file to attach.
   * Creates a staged session if one doesn't exist yet.
   */
  onFileAttached(file: File) {
    // If no session exists (not navigated to /s/:id and no staged session),
    // create a staged session for file uploads
    if (!this.sessionId() && !this.stagedSessionId()) {
      const newSessionId = uuidv4();
      this.stagedSessionId.set(newSessionId);

      // Add the session to cache so sidenav can show it
      const user = this.userService.currentUser();
      const userId = user?.user_id || 'anonymous';
      this.sessionService.addSessionToCache(newSessionId, userId);
    }
  }

  onMessageCancelled() {
    this.chatHttpService.cancelChatRequest();
  }

  /**
   * Called when the voice overlay closes.
   *
   * By this point, disconnect() has already set isVoiceActive = false,
   * so the messages computed stops merging voiceMessages. We persist
   * those messages into the message map (so they survive navigation)
   * and update the URL.
   */
  onVoiceClosed() {
    const voiceMsgs = this.voiceChatService.voiceMessages();
    if (voiceMsgs.length === 0) return;

    const sessionId = this.voiceChatService.getSessionId();
    if (!sessionId) return;

    // Persist voice messages into the message map so they survive
    // page navigation and show up when the overlay is gone.
    // Filter out any messages with no text (e.g. interrupted before first delta).
    this.messagesSignal.set(this.messageMapService.getMessagesForSession(sessionId));
    for (const msg of voiceMsgs) {
      const textBlock = msg.content.find(b => b.type === 'text');
      const text = textBlock?.text || '';
      if (!text) continue;
      this.messageMapService.addVoiceMessage(
        sessionId,
        msg.role as 'user' | 'assistant',
        text,
        msg.metadata ?? undefined,
      );
    }

    // Clear voice messages now that they're in the map — prevents any
    // change detection cycle from seeing both sources simultaneously.
    this.voiceChatService.clearVoiceMessages();

    // If there's no route session yet, navigate (fire-and-forget).
    // addSessionToCache MUST happen before navigation so the route
    // subscription recognises this as new and skips the API fetch
    // (same sequencing as ChatRequestService.navigateToSession).
    if (!this.effectiveSessionId()) {
      const user = this.userService.currentUser();
      const userId = user?.user_id || 'anonymous';
      this.sessionService.addSessionToCache(sessionId, userId);
      this.router.navigate(['s', sessionId], { replaceUrl: true });
    }

    // Generate title for new voice sessions (fire and forget)
    const firstUserMsg = voiceMsgs.find(m => m.role === 'user');
    const firstUserText = firstUserMsg?.content[0]?.text;
    if (firstUserText && this.sessionService.isNewSession(sessionId)) {
      this.chatHttpService.generateTitle(sessionId, firstUserText)
        .then((response) => {
          this.sessionService.updateSessionTitleInCache(sessionId, response.title);
        })
        .catch((err) => {
          console.warn('Failed to generate voice session title:', err);
        });
    }
  }

  toggleSettings() {
    this.isSettingsOpen.update(open => !open);
  }

  closeSettings() {
    this.isSettingsOpen.set(false);
  }

  /**
   * Load assistant by ID - only checks existence, not access
   * Access validation happens on backend when message is sent
   * @param assistantId - Assistant ID to load
   * @param fromPreferences - If true, this is from session preferences (skip message check)
   */
  private async loadAssistant(assistantId: string, fromPreferences: boolean = false): Promise<void> {
    // Validation: Only check messages for new attachments (not from preferences)
    if (!fromPreferences) {
      const sessionId = this.sessionId();
      if (sessionId && this.hasMessages()) {
        this.assistantError.set('Assistants can only be attached to new sessions');
        this.assistant.set(null);
        return;
      }
    }

    try {
      this.assistantError.set(null);
      this.isLoadingAssistant.set(true);
      // Only check existence (404), not access (403) - access validated on backend
      const loadedAssistant = await this.assistantService.getAssistant(assistantId);
      this.assistant.set(loadedAssistant);
    } catch (error: any) {
      console.error('Failed to load assistant:', error);
      
      // Only handle existence errors (404) - access errors (403) will be handled on backend
      if (error?.status === 404) {
        this.assistantError.set('Assistant not found');
        // If from preferences and assistant doesn't exist, optionally clear it
        if (fromPreferences) {
          // TODO: Optionally clear assistantId from session preferences via API
        }
      } else {
        // Other errors (network, etc.) - show generic error but don't block
        this.assistantError.set('Failed to load assistant');
      }
      
      // Don't clear assistant on error - let backend validate on message send
      // This allows user to see the assistant card even if frontend fetch fails
    } finally {
      this.isLoadingAssistant.set(false);
    }
  }

  /**
   * Clear assistantId from URL query parameters
   */
  private clearAssistantIdFromUrl(): void {
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { assistantId: null },
      queryParamsHandling: 'merge'
    });
  }

  /**
   * Start a new session with the same assistant.
   * Uses onSameUrlNavigation workaround since we may already be on '/'.
   */
  newAssistantSession(): void {
    const assistantId = this.assistant()?.assistantId;
    if (!assistantId) return;

    // If already on the root route (no sessionId), clear state and re-trigger assistant load
    if (!this.sessionId()) {
      // Already on a new session page — just reset signals to clear messages
      this.assistant.set(null);
      this.assistantError.set(null);
      // Re-set assistantId query param to trigger the assistant load effect
      this.router.navigate(['/'], {
        queryParams: { assistantId },
        queryParamsHandling: 'replace',
      });
      return;
    }

    // Navigate from an existing session to a fresh one with the assistant
    this.router.navigate(['/'], { queryParams: { assistantId } });
  }

  /**
   * Navigate to the assistant edit page.
   */
  editAssistant(): void {
    const assistantId = this.assistant()?.assistantId;
    if (assistantId) {
      this.router.navigate(['/assistants', assistantId, 'edit']);
    }
  }

  /**
   * Open the share assistant dialog.
   */
  shareAssistant(): void {
    const assistant = this.assistant();
    if (!assistant) return;

    this.dialog.open<unknown, ShareAssistantDialogData>(ShareAssistantDialogComponent, {
      data: { assistant },
      hasBackdrop: false,
    });
  }

  /**
   * Close/remove the assistant from the conversation.
   * Only works for new conversations (no messages) with assistants from query params.
   */
  closeAssistant(): void {
    // Safety check: only allow closing if conditions are met
    if (!this.canCloseAssistant()) {
      return;
    }

    // Clear the assistant and error state
    this.assistant.set(null);
    this.assistantError.set(null);

    // Clear the query parameter from URL
    this.clearAssistantIdFromUrl();
  }
}
