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
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { Message } from '../../services/models/message.model';
import { MessageListComponent } from '../message-list/message-list.component';
import { ChatInputComponent } from '../chat-input/chat-input.component';
import { AnimatedTextComponent } from '../../../components/animated-text';
import { ParagraphSkeletonComponent } from '../../../components/paragraph-skeleton';
import { Topnav } from '../../../components/topnav/topnav';
import { SidenavService } from '../../../services/sidenav/sidenav.service';
import { ArtifactStateService } from '../../services/artifacts/artifact-state.service';
import { Assistant } from '../../../assistants/models/assistant.model';
import { AssistantCardComponent } from '../../../assistants/components/assistant-card.component';
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
    NgIcon,
    AssistantCardComponent,
    AssistantIndicatorComponent,
    SessionCostBadgeComponent,
    VoiceOverlayComponent,
  ],
  providers: [provideIcons({ heroXMark })],
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
  messageSubmitted = output<{ content: string; timestamp: Date; fileUploadIds?: string[] }>();
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

  // Computed signals
  protected readonly hasMessages = computed(() => this.messages().length > 0);
  protected readonly showSkeleton = computed(
    () => this.isLoadingSession() && !this.hasMessages()
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

  // Event handlers
  onMessageSubmitted(event: { content: string; timestamp: Date; fileUploadIds?: string[] }) {
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
