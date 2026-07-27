import {
  Component,
  ChangeDetectionStrategy,
  effect,
  inject,
  input,
  output,
  computed,
  viewChild,
} from '@angular/core';
import { Message } from '../../services/models/message.model';
import { MessageListComponent } from '../message-list/message-list.component';
import { ChatInputComponent } from '../chat-input/chat-input.component';
import { AnimatedTextComponent } from '../../../components/animated-text';
import { ParagraphSkeletonComponent } from '../../../components/paragraph-skeleton';
import { Topnav } from '../../../components/topnav/topnav';
import { SidenavService } from '../../../services/sidenav/sidenav.service';
import { ArtifactStateService } from '../../services/artifacts/artifact-state.service';
import { Assistant } from '../../../assistants/models/assistant.model';
import { Agent, AgentRunnability } from '../../../agents/models/agent.model';
import {
  AgentLaunchCardComponent,
  AgentLaunchCardView,
  agentLaunchCardView,
} from '../../../agents/components/agent-launch-card.component';
import { AssistantIndicatorComponent } from '../assistant-indicator/assistant-indicator.component';
import { SessionCostBadgeComponent } from '../session-cost-badge/session-cost-badge.component';
import { VoiceOverlayComponent } from '../voice-overlay';
import { VoiceChatService } from '../../services/voice';
import { ChatStateService } from '../../services/chat/chat-state.service';

/**
 * Configuration options for ChatContainerComponent.
 * Controls which features are enabled based on usage context.
 */
export interface ChatContainerConfig {
  /** Show the top navigation bar (full-page mode only) */
  showTopnav: boolean;
  /** Show the greeting/empty state */
  showEmptyState: boolean;
  /** Allow closing the assistant card */
  allowCloseAssistant: boolean;
  /** Show file attachment controls in chat input */
  showFileControls: boolean;
  /** Show voice mode toggle in chat input */
  showVoiceControl: boolean;
  /** Show settings/tools button in chat input */
  showSettingsControl: boolean;
  /** Custom greeting message (overrides default) */
  customGreeting?: string;
  /** Enable embedded mode (flex layout, no fixed positioning) */
  embeddedMode: boolean;
  /** Enable full-page mode (fixed positioning with sidenav awareness) */
  fullPageMode: boolean;
}

/**
 * Reusable chat container component that can be used in both:
 * - Full-page mode (session page with fixed positioning and sidenav awareness)
 * - Embedded mode (assistant preview with flex layout)
 */
@Component({
  selector: 'app-chat-container',
  standalone: true,
  imports: [
    MessageListComponent,
    ChatInputComponent,
    AnimatedTextComponent,
    ParagraphSkeletonComponent,
    Topnav,
    AgentLaunchCardComponent,
    AssistantIndicatorComponent,
    SessionCostBadgeComponent,
    VoiceOverlayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './chat-container.component.html',
  styleUrl: './chat-container.component.css',
})
export class ChatContainerComponent {
  // Inject sidenav service for full-page mode positioning
  protected sidenavService = inject(SidenavService);
  private artifactState = inject(ArtifactStateService);
  private voiceChatService = inject(VoiceChatService);
  protected readonly isVoiceActive = this.voiceChatService.isVoiceActive;

  // Child component reference for scroll functionality
  private messageListComponent = viewChild(MessageListComponent);

  private readonly chatState = inject(ChatStateService);

  // Non-composer submit paths (e.g. an MCP App widget's ui/message) bump
  // ChatStateService.scrollToLastUserTick to get the same "scroll the new
  // user message to the top" affordance the composer triggers in
  // onMessageSubmitted. Skip the initial 0 so we don't scroll on mount.
  private readonly _scrollOnExternalSubmit = effect(() => {
    const tick = this.chatState.scrollToLastUserTick();
    if (tick === 0) return;
    setTimeout(
      () => this.messageListComponent()?.scrollToLastUserMessage(),
      100,
    );
  });

  // Required inputs
  messages = input.required<Message[]>();
  sessionId = input<string | null>(null);

  // Optional inputs
  assistant = input<Assistant | null>(null);
  /**
   * The governed Agent behind this conversation, when it resolves (`agentId ==
   * assistantId`). The launch card reads from this rather than `assistant` because
   * tagline, publisher, category and capabilities exist only on the Agent shape — see
   * `agentLaunchCardView`.
   */
  agent = input<Agent | null>(null);
  /** D6, fetched best-effort by the session page; the card omits the line without it. */
  runnability = input<AgentRunnability | null>(null);
  assistantError = input<string | null>(null);
  isLoadingAssistant = input<boolean>(false);
  isChatLoading = input<boolean>(false);
  isLoadingSession = input<boolean>(false);
  streamingMessageId = input<string | null>(null);
  greetingMessage = input<string>('How can I help you today?');

  // Configuration with defaults
  config = input<Partial<ChatContainerConfig>>({});

  protected readonly resolvedConfig = computed<ChatContainerConfig>(() => ({
    showTopnav: false,
    showEmptyState: true,
    allowCloseAssistant: true,
    showFileControls: true,
    showVoiceControl: true,
    showSettingsControl: true,
    embeddedMode: false,
    fullPageMode: false,
    ...this.config(),
  }));

  // Output events
  // `mentionAgentId` rides through untouched (Marketplace D11): the container is a
  // layout shell, and dropping the field here would silently turn every `@`-mention
  // back into a plain turn.
  messageSubmitted = output<{ content: string; timestamp: Date; fileUploadIds?: string[]; mentionAgentId?: string }>();
  continueRequested = output<void>();
  messageCancelled = output<void>();
  fileAttached = output<File>();
  settingsToggled = output<void>();
  assistantClosed = output<void>();
  starterSelected = output<string>();
  assistantNewSession = output<void>();
  assistantEdit = output<void>();
  assistantShare = output<void>();
  voiceClosed = output<void>();

  /**
   * What the launch card renders.
   *
   * Prefers the Agent shape and falls back to the Assistant, because the two loads are
   * independent: `loadAssistant` resolves first and `loadAgentBindings` follows, and the
   * Agent call is allowed to fail outright (the `/agents` surface can be off). Without
   * the fallback the card would flicker in a beat late, or never paint at all in a
   * flag-off environment — for a record that is the same record either way.
   *
   * The fallback is deliberately `listed: false`: an Assistant carries no listing, so
   * neither store affordance is offered rather than guessed at.
   */
  protected readonly launchCardView = computed<AgentLaunchCardView | null>(() => {
    const agent = this.agent();
    if (agent) return agentLaunchCardView(agent);

    const assistant = this.assistant();
    if (!assistant) return null;
    return {
      agentId: assistant.assistantId,
      name: assistant.name,
      description: assistant.description,
      ownerName: assistant.ownerName,
      emoji: assistant.emoji,
      starters: assistant.starters ?? [],
      listed: false,
    };
  });

  // Computed signals
  protected readonly hasMessages = computed(() => this.messages().length > 0);
  protected readonly showSkeleton = computed(
    () => this.isLoadingSession() && !this.hasMessages()
  );
  /** The greeting/empty state (new chat, nothing loading). No top bar here. */
  protected readonly isEmptyState = computed(
    () =>
      !this.showSkeleton() &&
      !this.hasMessages() &&
      this.resolvedConfig().showEmptyState
  );
  /**
   * Whether the fixed top bar should render. Kept as a single computed so one
   * persistent <app-topnav> spans the skeleton→loaded transition instead of
   * remounting — a remount restarts the fixed wrapper's `left` transition,
   * which reads as the whole bar sweeping across the screen.
   */
  protected readonly showChatTopnav = computed(
    () =>
      this.resolvedConfig().fullPageMode &&
      this.resolvedConfig().showTopnav &&
      !this.isEmptyState()
  );
  protected readonly canCloseAssistant = computed(
    () =>
      this.resolvedConfig().allowCloseAssistant &&
      !this.hasMessages() &&
      !!this.assistant()
  );
  protected readonly isSidenavCollapsed = computed(() =>
    this.sidenavService.isCollapsed()
  );
  /** True while the docked artifact pane is open — the fixed footer /
   *  topnav reserve right-side space so the pane doesn't cover them. */
  protected readonly artifactPanelOpen = computed(
    () => this.artifactState.openArtifact() !== null
  );
  protected readonly isAssistantOwner = computed(() => {
    const a = this.assistant();
    if (!a) return false;
    // Backend excludes ownerId from responses for privacy.
    // isSharedWithMe is true when the assistant belongs to someone else.
    return !a.isSharedWithMe;
  });

  /**
   * Anchor the latest user message at the top of the viewport. Exposed for
   * the session page's navigation scroll policy (first open of a
   * conversation lands on its latest turn, instantly); the composer submit
   * path below uses the same anchor with smooth scrolling.
   */
  scrollToLastUserMessage(behavior: ScrollBehavior = 'smooth'): void {
    this.messageListComponent()?.scrollToLastUserMessage(behavior);
  }

  // Event handlers
  onMessageSubmitted(event: { content: string; timestamp: Date; fileUploadIds?: string[]; mentionAgentId?: string }) {
    this.messageSubmitted.emit(event);

    // Wait for DOM to update (user message to be added) then scroll to it
    setTimeout(() => {
      this.messageListComponent()?.scrollToLastUserMessage();
    }, 100);
  }

  onMessageCancelled() {
    this.messageCancelled.emit();
  }

  onFileAttached(file: File) {
    this.fileAttached.emit(file);
  }

  onSettingsToggled() {
    this.settingsToggled.emit();
  }

  onAssistantClosed() {
    this.assistantClosed.emit();
  }

  onStarterSelected(starter: string) {
    this.starterSelected.emit(starter);
    // Submit the starter as a message
    this.onMessageSubmitted({ content: starter, timestamp: new Date() });
  }

  onAssistantNewSession() {
    this.assistantNewSession.emit();
  }

  onAssistantEdit() {
    this.assistantEdit.emit();
  }

  onAssistantShare() {
    this.assistantShare.emit();
  }

  onVoiceClosed() {
    this.voiceClosed.emit();
  }
}
