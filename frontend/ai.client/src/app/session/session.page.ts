import { Component, inject, effect, Signal, signal, computed, viewChild, OnDestroy, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { v4 as uuidv4 } from 'uuid';
import { ChatRequestService } from './services/chat/chat-request.service';
import { MessageMapService } from './services/session/message-map.service';
import { Message } from './services/models/message.model';
import { SessionService } from './services/session/session.service';
import { ScrollPositionService } from './services/session/scroll-position.service';
import { ChatStateService } from './services/chat/chat-state.service';
import { SidenavService } from '../services/sidenav/sidenav.service';
import { HeaderService } from '../services/header/header.service';
import { ModelService } from './services/model/model.service';
import { ModelSettings } from '../components/model-settings/model-settings';
import { UserService } from '../auth/user.service';
import { ChatHttpService } from './services/chat/chat-http.service';
import { StreamParserService } from './services/chat/stream-parser.service';
import { CompactionSummaryService } from './services/chat/compaction-summary.service';
import { ArtifactStateService } from './services/artifacts/artifact-state.service';
import { ArtifactHttpService } from './services/artifacts/artifact-http.service';
import { McpAppStateService } from './services/mcp-apps/mcp-app-state.service';
import { McpAppCardStateService } from './services/mcp-apps/mcp-app-card-state.service';
import { McpAppCardHttpService } from './services/mcp-apps/mcp-app-card-http.service';
import { McpAppConsentService } from './services/mcp-apps/mcp-app-consent.service';
import { Dialog } from '@angular/cdk/dialog';
import { AssistantService } from '../assistants/services/assistant.service';
import { Assistant } from '../assistants/models/assistant.model';
import { AgentService } from '../agents/services/agent.service';
import { Agent, AgentRunnability } from '../agents/models/agent.model';
import { ToolService } from '../services/tool/tool.service';
import { SkillService } from '../services/skill/skill.service';
import { ChatContainerComponent, ChatContainerConfig } from './components/chat-container/chat-container.component';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../assistants/components/share-assistant-dialog.component';
import { VoiceChatService } from './services/voice';
import { SystemPromptsService } from '../services/system-prompts/system-prompts.service';
import { OAuthConsentService } from '../services/oauth-consent/oauth-consent.service';

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
  private compactionSummary = inject(CompactionSummaryService);
  private artifactState = inject(ArtifactStateService);
  private mcpAppState = inject(McpAppStateService);
  private mcpAppCardState = inject(McpAppCardStateService);
  private mcpAppCardHttp = inject(McpAppCardHttpService);
  private mcpAppConsent = inject(McpAppConsentService);
  private oauthConsent = inject(OAuthConsentService);
  private artifactHttp = inject(ArtifactHttpService);
  private assistantService = inject(AssistantService);
  private agentService = inject(AgentService);
  private toolService = inject(ToolService);
  private skillService = inject(SkillService);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private voiceChatService = inject(VoiceChatService);
  private systemPromptsService = inject(SystemPromptsService);
  private scrollPositions = inject(ScrollPositionService);
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);

  private chatContainer = viewChild(ChatContainerComponent);

  /**
   * The app shell's scroll container (#app-scroll-container in app.html).
   * Since the shell became a real scroll container (frosted sticky nav),
   * the window itself never scrolls — all scroll save/restore reads and
   * writes go through this element. Browser-only callers guard isBrowser.
   */
  private get scrollContainer(): HTMLElement | null {
    return document.getElementById('app-scroll-container');
  }

  /**
   * The conversation whose scroll position we're currently tracking.
   * Captured into ScrollPositionService the moment the user navigates away
   * (route change or component teardown) — see the scroll policy note on
   * the route subscription.
   */
  private lastViewedSessionId: string | null = null;

  sessionId = signal<string | null>(null);
  assistantIdFromQuery = signal<string | null>(null);

  assistant = signal<Assistant | null>(null);
  assistantError = signal<string | null>(null);
  // Agent Designer: the governed Agent behind this conversation (agentId ==
  // assistantId), when the /agents surface is enabled and it resolves. Drives
  // the per-primitive picker locks; null for plain chat or a legacy assistant.
  agent = signal<Agent | null>(null);
  /**
   * D6 for the launch card, when it resolves. Deliberately a third signal rather than a
   * field on `agent`: it fans out across the viewer's model/tool/skill catalogs, so it
   * settles in after the card has already painted — the same two-load split the store's
   * detail page uses.
   */
  runnability = signal<AgentRunnability | null>(null);
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

  // Streaming indicator for the conversation on screen. Parser state is
  // per-session, so a conversation streaming in the background never
  // animates the one being viewed.
  readonly streamingMessageId = computed<string | null>(() => {
    const id = this.sessionId();
    return id ? this.streamParserService.streamingMessageIdFor(id)() : null;
  });

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
    // Keep the viewed-session facades in ChatStateService (loading, cost
    // badge, Continue affordance, Stop button) pointed at the conversation
    // on screen. Uses the effective id so a staged session (file attached
    // before the first message) counts as viewed too.
    effect(() => {
      this.chatStateService.setViewedSession(this.effectiveSessionId());
    });

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

    // Hydrate active system prompt from session preferences. We treat the
    // sessionId itself as the trigger so that switching from Session A to a
    // brand-new Session B (whose metadata hasn't loaded yet) resets the
    // chip rather than leaking A's selection into B.
    //
    // The service tracks which session a local selection is bound to, so
    // a freshly-claimed home-page selection isn't wiped when the new
    // session's metadata arrives without preferences yet.
    effect(() => {
      const id = this.sessionId();
      const session = this.sessionConversation();

      if (!id || session?.sessionId !== id) {
        this.systemPromptsService.hydrateFromSession(id, null);
        return;
      }

      this.systemPromptsService.hydrateFromSession(
        id,
        session?.preferences?.selectedPromptId ?? null
      );
    });

    // Seed the session cost + context aggregates from session metadata so
    // the badge shows totals immediately on revisit. Cleared first on route
    // change (below) to avoid briefly showing stale numbers from a previous
    // session.
    effect(() => {
      const session = this.sessionConversation();
      if (!session?.sessionId) return;
      this.chatStateService.seedSessionAggregates(session.sessionId, {
        totalCost: session.totalCost,
        lastContextTokens: session.lastContextTokens,
        contextWindow: session.contextWindow,
      });
      // Refresh-survival for the max_tokens "Continue" affordance: the
      // truncated partial is already in restored history; this flag is the
      // missing piece. Only set true here — the live stream_error path owns
      // the in-turn signal, so we never clobber a live true with stale
      // metadata.
      if (session.lastTurnContinuable) {
        this.chatStateService.setLastTurnContinuable(session.sessionId, true);
      }
      // Refresh-survival for the interrupted-turn chip: the partial (or a
      // placeholder) is already in restored history; the marker + reason are
      // the missing pieces. Only set true here — a new send / live abort owns
      // the in-turn signal, so we never clobber a live true with stale
      // metadata.
      if (session.lastTurnInterrupted) {
        this.chatStateService.setLastTurnInterrupted(
          session.sessionId,
          true,
          session.lastTurnInterruptReason ?? 'unknown',
        );
      }
    });

    // Hydrate the compaction summary indicator from persisted session
    // metadata. `seedFromHydration` is idempotent and won't clobber live
    // increments, so re-runs of this effect (e.g. when other fields on
    // currentSession update) are harmless. The route subscription resets
    // the service before triggering the metadata fetch, so cross-session
    // bleed is impossible.
    effect(() => {
      const session = this.sessionConversation();
      if (!session?.sessionId || session.sessionId !== this.sessionId()) return;
      const total = session.totalSummarizedTurns ?? 0;
      if (total > 0) {
        this.compactionSummary.seedFromHydration(total);
      }
    });

    // Single source of truth: the URL's `assistantId` query parameter.
    //
    // The URL is authoritative for which assistant is attached to the
    // current view. Session preferences on the backend still record the
    // attached assistant (so we can rebuild the URL after a user lands on
    // a bare `/s/:id` URL from a bookmark or legacy link), but that
    // rebuild is handled by a dedicated self-heal redirect below — not by
    // reading preferences here. Keeping this effect URL-only removes an
    // entire class of races around metadata fetch timing and component
    // recreation (see #205).
    effect(() => {
      const queryAssistantId = this.assistantIdFromQuery();
      const loadedAssistant = this.assistant();

      if (queryAssistantId) {
        // Already loaded — avoid a redundant fetch and the transient null
        // state while the fetch would resolve.
        if (loadedAssistant?.assistantId === queryAssistantId) {
          return;
        }
        // Existence check only; access is validated on the backend when
        // the next message is sent.
        this.loadAssistant(queryAssistantId).catch(error => {
          console.error('Failed to load assistant from query param:', error);
        });
        return;
      }

      // No assistant in the URL — clear any stale local state from a prior load.
      if (loadedAssistant || this.assistantError() || this.agent()) {
        this.assistant.set(null);
        this.assistantError.set(null);
        this.agent.set(null);
        this.runnability.set(null);
      }
      // Always release the picker locks. They live in root singleton services
      // that OUTLIVE this component, so a freshly-created "new chat" component
      // (whose own assistant()/agent() signals start null, making the guard
      // above false) must still release locks the previous conversation left
      // behind. Idempotent — a no-op when nothing is locked.
      this.clearAgentBindingLocks();
    });

    // Self-heal effect: when the user lands on `/s/:id` without an
    // `assistantId` query param but the session's stored preferences carry
    // one (bookmarks, legacy URLs, shared session links), redirect to the
    // same session with the param filled in. From that point on, the URL
    // is the sole source of truth for the assistant-loading effect above.
    //
    // Intentionally narrow: we only redirect when the URL is empty. If the
    // URL already carries an `assistantId`, we trust it — including when
    // it differs from preferences (the backend will reject a conflict on
    // the next message).
    effect(() => {
      const session = this.sessionConversation();
      const sessionAssistantId = session?.preferences?.assistantId;
      const currentSessionId = this.sessionId();
      const queryAssistantId = this.assistantIdFromQuery();

      if (
        currentSessionId &&
        session?.sessionId === currentSessionId &&
        sessionAssistantId &&
        !queryAssistantId
      ) {
        this.router.navigate([], {
          relativeTo: this.route,
          queryParams: { assistantId: sessionAssistantId },
          queryParamsHandling: 'merge',
          replaceUrl: true,
        });
      }
    });

    // Subscribe to route parameter changes.
    //
    // Deliberately does NOT abort an in-flight stream for the session being
    // navigated away from: the stream keeps running in the background,
    // syncing into its own session's message map (per-session parser +
    // per-session sync effect), and the backend completes and persists the
    // turn either way. Only the Stop button aborts, and only its own session.
    //
    // Scroll policy on conversation change: remember where the user was in
    // the conversation they're leaving, reset to the top while the next one
    // loads (the leftover window offset is meaningless against different
    // content), then restore below — remembered position if we have one,
    // otherwise anchor the latest turn (restoreScrollPosition).
    this.routeSubscription = this.route.paramMap.subscribe(async params => {
      const id = params.get('sessionId');

      if (this.isBrowser && this.lastViewedSessionId !== id) {
        if (this.lastViewedSessionId) {
          this.scrollPositions.save(this.lastViewedSessionId, this.scrollContainer?.scrollTop ?? 0);
        }
        this.scrollContainer?.scrollTo({ top: 0, behavior: 'auto' });
      }
      this.lastViewedSessionId = id;

      this.sessionId.set(id);

      // Cost/context badge and Continue-affordance state is per-session in
      // ChatStateService, and the viewed-session effect above repoints the
      // facades — no cross-session clearing needed here anymore.

      // Compaction summary is session-scoped — clear before loading the
      // next session's metadata so the previous session's totals don't
      // bleed in. The hydration effect above will reseed from
      // currentSession.totalSummarizedTurns once the metadata fetch lands.
      this.compactionSummary.reset();

      // Artifacts are session-scoped — clear before the next session
      // loads so a prior session's cards don't bleed in, then re-hydrate
      // from the app-api list endpoint below.
      this.artifactState.reset();

      // MCP App frames persist for the conversation's lifetime per the
      // scoping doc; teardown is on conversation change. Clear before the
      // next load — loadMessagesForSession re-seeds from the persisted
      // `uiResources` sidecar on the messages response so frames survive a
      // refresh (the inline `ui_resource` event itself only arrives live).
      this.mcpAppState.reset();

      // Option A (PR #6): app-initiated tool cards DO re-hydrate (the
      // broker is in-memory). Any open consent prompt for the prior
      // conversation is dropped fail-closed.
      this.mcpAppCardState.reset();
      this.mcpAppConsent.reset();

      // OAuth "Authorization needed" prompts are held in a root singleton
      // keyed by providerId (not sessionId), so a prior conversation's
      // request would otherwise bleed onto the next session — including the
      // blank welcome screen. Clear fail-closed on conversation change;
      // loadMessagesForSession below re-seeds this session's own pending
      // interrupts from the persisted server metadata.
      this.oauthConsent.clear();

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

        // Re-hydrate artifact cards for this session so they survive a
        // refresh / deep link. Best-effort and non-blocking: a 404 (the
        // artifacts feature is off for this environment) or any network
        // error just means no cards — never disrupt the session.
        this.hydrateArtifacts(id);
        this.hydrateMcpAppCards(id);

        // Messages are in the map and rendering — position the viewport
        // per the scroll policy (remembered spot, else latest turn).
        this.restoreScrollPosition(id);
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
    // Leaving the conversation view entirely (e.g. to an admin page, or the
    // `/` ↔ `/s/:id` transitions that recreate this component) — remember
    // where the user was so navigating back restores it.
    if (this.isBrowser) {
      const id = this.sessionId();
      if (id) {
        this.scrollPositions.save(id, this.scrollContainer?.scrollTop ?? 0);
      }
    }
    this.routeSubscription?.unsubscribe();
    this.queryParamSubscription?.unsubscribe();
  }

  /**
   * Position the viewport for a freshly navigated-to conversation:
   * the remembered offset when the user has been here this SPA session,
   * otherwise the latest turn (last user message anchored at top — the same
   * framing the composer submit uses, so "returning to" and "just asked in"
   * a conversation look alike). Instant, never smooth: animating a jump
   * across a whole conversation is noise.
   *
   * Runs two animation frames after the messages landed in the map so the
   * message DOM (and the last-turn min-height that provides the scroll
   * room) has committed and the full scroll height exists; bails if the
   * user has already navigated elsewhere.
   */
  private restoreScrollPosition(sessionId: string): void {
    if (!this.isBrowser) return;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (this.sessionId() !== sessionId) return;

        const saved = this.scrollPositions.get(sessionId);
        if (saved !== undefined) {
          this.scrollContainer?.scrollTo({ top: saved, behavior: 'auto' });
        } else {
          this.chatContainer()?.scrollToLastUserMessage('auto');
        }
      });
    });
  }

  /**
   * Best-effort: pull the session's artifacts and seed the registry so
   * cards render on load. `seedFromHydration` is non-clobbering, so a
   * slow response that lands after a live `artifact` event won't undo a
   * newer version. A guard re-checks the active session because the
   * fetch is async and the user may have navigated away.
   */
  private hydrateArtifacts(sessionId: string): void {
    this.artifactHttp
      .listSessionArtifacts(sessionId)
      .then(artifacts => {
        if (artifacts.length && this.sessionId() === sessionId) {
          this.artifactState.seedFromHydration(artifacts);
        }
      })
      .catch(() => {
        // Feature disabled (404) or transient error — no cards, no noise.
      });
  }

  /**
   * Best-effort: pull persisted app-initiated tool cards and seed the
   * registry so they survive a reload (the PR #5 broker is in-memory).
   * Mirrors {@link hydrateArtifacts}: non-clobbering, session-guarded,
   * silent on 404 (MCP Apps host flag off) or transient error.
   */
  private hydrateMcpAppCards(sessionId: string): void {
    this.mcpAppCardHttp
      .listSessionCards(sessionId)
      .then(cards => {
        if (cards.length && this.sessionId() === sessionId) {
          this.mcpAppCardState.seedFromHydration(cards);
        }
      })
      .catch(() => {
        // Feature disabled (404) or transient error — no cards, no noise.
      });
  }

  onMessageSubmitted(message: { content: string, timestamp: Date, fileUploadIds?: string[], mentionAgentId?: string }) {
    // Use the effective session ID (route sessionId or staged sessionId)
    const sessionIdToUse = this.effectiveSessionId();

    // The URL's `assistantId` query param is the sole source of truth. It's
    // set on initial navigation (assistant card, share link) and kept in
    // sync by the self-heal redirect in the constructor for existing
    // sessions opened without one. Falling back to the in-memory
    // `assistant()` signal guards the brief window during the `/` → `/s/:id`
    // route transition where the component is recreated and the query
    // param hasn't yet propagated to the new instance.
    const assistantIdToUse =
      this.assistantIdFromQuery() || this.assistant()?.assistantId || undefined;

    // Loading state is set inside submitChatRequest once the (possibly
    // freshly generated) session id is known — it's per-session now.

    // Submit the chat request with file upload IDs and assistant ID if present.
    // A `@`-mention (Marketplace D11) rides alongside rather than replacing the bound
    // assistant: it runs this one turn, the URL keeps whatever the conversation is bound
    // to, and the next unmentioned message goes back to that.
    this.chatRequestService.submitChatRequest(
      message.content,
      sessionIdToUse,
      message.fileUploadIds,
      assistantIdToUse,
      message.mentionAgentId
    ).catch((error) => {
      console.error('Error sending chat request:', error);
    });

    // Clear the staged session ID after submission (it's now a real session)
    if (this.stagedSessionId()) {
      this.stagedSessionId.set(null);
    }
  }

  /**
   * "Continue" affordance on a max_tokens-truncated assistant message.
   * NOT a new user turn: it resumes the truncated assistant message via the
   * continuation path (no visible user bubble, empty prompt) so the model
   * picks up where it stopped instead of re-answering the original request.
   */
  onContinueRequested() {
    const sessionIdToUse = this.effectiveSessionId();
    const assistantIdToUse =
      this.assistantIdFromQuery() || this.assistant()?.assistantId || undefined;
    this.chatRequestService
      .continueTruncatedTurn(sessionIdToUse, assistantIdToUse)
      .catch((error) => {
        console.error('Error continuing truncated turn:', error);
      });
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
    // Stop only the viewed conversation's stream; a conversation streaming
    // in the background is unaffected.
    const sessionId = this.effectiveSessionId();
    if (sessionId) {
      this.chatHttpService.cancelChatRequest(sessionId);
    }
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
      // Carry the assistant id forward if one is attached to this view —
      // keeps the URL the single source of truth after voice ends.
      const assistantId = this.assistantIdFromQuery() || this.assistant()?.assistantId;
      this.router.navigate(['s', sessionId], {
        replaceUrl: true,
        queryParams: assistantId ? { assistantId } : {},
      });
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
   * Load assistant by ID - only checks existence, not access.
   * Access and mid-session conflicts are validated on the backend when the
   * next message is sent (the inference-api compares the request's
   * rag_assistant_id against the session's stored assistant and rejects
   * mismatches). Doing the same check here is unreliable because the
   * component is recreated on the `/` → `/s/:id` route transition, so any
   * "session has messages" guard fires on the normal first-turn flow and
   * clears the assistant that the user just opened (#205).
   */
  private async loadAssistant(assistantId: string): Promise<void> {
    try {
      this.assistantError.set(null);
      this.isLoadingAssistant.set(true);
      // Only check existence (404), not access (403) - access validated on backend
      const loadedAssistant = await this.assistantService.getAssistant(assistantId);
      this.assistant.set(loadedAssistant);
      // Reflect the Agent's governed bindings in the chat-input (Agent Designer).
      // Best-effort and independent of the assistant fetch above.
      await this.loadAgentBindings(assistantId);
    } catch (error: any) {
      console.error('Failed to load assistant:', error);
      
      // Only handle existence errors (404) - access errors (403) will be handled on backend
      if (error?.status === 404) {
        this.assistantError.set('Assistant not found');
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
   * Fetch the governed Agent behind this conversation and lock the chat-input
   * pickers to its bindings (Agent Designer). `agentId == assistantId`, so the
   * same query-param id resolves the Agent. Best-effort: the /agents surface may
   * be disabled (404) or the assistant may be a legacy assistant with no
   * bindings — in every failure case the pickers stay free-select (locks cleared),
   * and the conversation is never blocked on this. The backend still governs
   * bindings at invocation regardless, so this is UI honesty, not enforcement.
   */
  private async loadAgentBindings(assistantId: string): Promise<void> {
    try {
      const agent = await this.agentService.getAgent(assistantId);
      this.agent.set(agent);
      this.applyAgentBindingLocks(agent);
    } catch {
      this.agent.set(null);
      this.runnability.set(null);
      this.clearAgentBindingLocks();
      return;
    }

    // D6 for the launch card's run line. Not awaited by anything the conversation needs,
    // and a failure leaves the line absent rather than erroring — "will this run for you?"
    // is advisory here exactly as it is on the store's detail page.
    try {
      this.runnability.set(await this.agentService.getRunnability(assistantId));
    } catch {
      this.runnability.set(null);
    }
  }

  /** Lock each picker to the Agent's binding, or release it when unbound. */
  private applyAgentBindingLocks(agent: Agent): void {
    const modelId = agent.modelConfig?.modelId;
    if (modelId) {
      this.modelService.lockToAgentModel(modelId);
    } else {
      this.modelService.clearAgentModelLock();
    }

    const toolIds = agent.bindings.filter(b => b.kind === 'tool').map(b => b.ref);
    if (toolIds.length > 0) {
      this.toolService.lockToAgentTools(toolIds);
    } else {
      this.toolService.clearAgentLock();
    }

    const skillIds = agent.bindings.filter(b => b.kind === 'skill').map(b => b.ref);
    if (skillIds.length > 0) {
      this.skillService.lockToAgentSkills(skillIds);
      // Load the accessible skills so the locked rows can render their names.
      // Only fires when an Agent actually binds skills (a skills-enabled
      // context), so there is no /skills fetch in flag-off environments.
      if (!this.skillService.initialized()) {
        void this.skillService.loadSkills();
      }
    } else {
      this.skillService.clearAgentLock();
    }
  }

  /** Release every Agent picker lock (plain chat / navigating away). */
  private clearAgentBindingLocks(): void {
    this.modelService.clearAgentModelLock();
    this.toolService.clearAgentLock();
    this.skillService.clearAgentLock();
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
   * Navigate to the Designer for the Agent driving this session.
   *
   * The id is the same record either way — the compat mapping renders a legacy Assistant
   * *as* an Agent, so `/agents/:id/edit` opens what `/assistants/:id/edit` used to. Routed
   * directly rather than through the redirect so the address bar never shows the retired
   * path.
   */
  editAssistant(): void {
    const assistantId = this.assistant()?.assistantId;
    if (assistantId) {
      this.router.navigate(['/agents', assistantId, 'edit']);
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
