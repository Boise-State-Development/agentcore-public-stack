import { Component, computed, input, output, inject, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser, NgTemplateOutlet } from '@angular/common';
import { Message } from '../../services/models/message.model';
import type { Artifact } from '../../services/artifacts/artifact.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { MessageMetadataBadgesComponent } from './components/message-metadata-badges.component';
import { MessageActionsComponent } from './components/message-actions.component';
import { CitationDisplayComponent } from '../citation-display/citation-display.component';
import { PulsatingLoaderComponent } from '../../../components/pulsating-loader.component';
import { OAuthConsentPromptComponent } from './components/oauth-consent-prompt/oauth-consent-prompt.component';
import { ToolApprovalPromptComponent } from './components/tool-approval-prompt/tool-approval-prompt.component';
import { CompactionSummaryComponent } from './components/compaction-summary/compaction-summary.component';
import { ArtifactCardComponent } from './components/artifact/artifact-card.component';
import { ArtifactPanelComponent } from './components/artifact/artifact-panel.component';
import { ArtifactStateService } from '../../services/artifacts/artifact-state.service';
import { McpAppCardComponent } from './components/mcp-app-card/mcp-app-card.component';
import { AgentFeedbackLinkComponent } from '../../../agents/components/agent-feedback-link.component';
import { McpAppCardStateService } from '../../services/mcp-apps/mcp-app-card-state.service';
import {
  OAuthConsentRequest,
  OAuthConsentService,
} from '../../../services/oauth-consent/oauth-consent.service';
import {
  ToolApprovalRequest,
  ToolApprovalService,
} from '../../../services/tool-approval/tool-approval.service';
import { CompactionSummaryService } from '../../services/chat/compaction-summary.service';
import { ChatStateService } from '../../services/chat/chat-state.service';

@Component({
  selector: 'app-message-list',
  imports: [
    NgTemplateOutlet,
    UserMessageComponent,
    AssistantMessageComponent,
    MessageActionsComponent,
    MessageMetadataBadgesComponent,
    CitationDisplayComponent,
    PulsatingLoaderComponent,
    OAuthConsentPromptComponent,
    ToolApprovalPromptComponent,
    CompactionSummaryComponent,
    ArtifactCardComponent,
    ArtifactPanelComponent,
    McpAppCardComponent,
    AgentFeedbackLinkComponent,
  ],
  templateUrl: './message-list.component.html',
  styleUrl: './message-list.component.css',
})
export class MessageListComponent {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);

  // Constants for scroll behavior and layout
  private readonly HEADER_HEIGHT = 64;
  private readonly SCROLL_PADDING = 16;

  messages = input.required<Message[]>();
  isChatLoading = input<boolean>(false);
  streamingMessageId = input<string | null>(null);
  embeddedMode = input<boolean>(false);

  /**
   * The published marketplace agent behind this conversation, when there is one — the
   * foot-of-conversation feedback link, and nothing else. Null for plain chat, a legacy
   * assistant, or an agent that is not published (D15.3).
   */
  feedbackAgent = input<{ id: string; name: string } | null>(null);

  /** The conversation itself, offered to attach to that feedback. */
  sessionId = input<string | null>(null);

  /** Bubbled up when the user clicks "Continue" on a max_tokens-truncated
   *  assistant message. The page reuses the normal submit path with a
   *  canned prompt. */
  continueRequested = output<void>();

  private consentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private compactionSummary = inject(CompactionSummaryService);
  private artifactState = inject(ArtifactStateService);
  private mcpAppCardState = inject(McpAppCardStateService);
  private chatStateService = inject(ChatStateService);

  /** Persisted app-initiated tool cards, hydrated on reload (PR #6). */
  protected mcpAppCards = this.mcpAppCardState.cards;
  protected hasMcpAppCards = this.mcpAppCardState.hasCards;

  /**
   * The feedback link needs something to give feedback *about*, so it waits for a turn to
   * have happened, and stays out of the way while one is still streaming — it sits at the
   * very foot of the tail, and appearing under a half-written answer reads as part of it.
   */
  protected readonly showFeedbackLink = computed(
    () => !!this.feedbackAgent() && this.messages().length > 0 && !this.isChatLoading(),
  );

  /** Only the final message of a recoverable max_tokens turn gets the
   *  "Continue" affordance. Live-only state, never shown while a new
   *  response is streaming. */
  private readonly lastMessageId = computed<string | null>(() => {
    const m = this.messages();
    return m.length ? m[m.length - 1].id : null;
  });

  protected canContinueFor(messageId: string): boolean {
    return (
      this.chatStateService.lastTurnContinuable() &&
      !this.isChatLoading() &&
      messageId === this.lastMessageId()
    );
  }

  /** The interruption reason to surface on this message, or null. Only the
   *  final message of an interrupted turn gets the chip, and only while no
   *  new response is streaming — mirrors `canContinueFor`, and the
   *  last-message gate sanity-checks against the "completed anyway" race
   *  (a new turn clears the flag). `connection_lost` also drives a Continue
   *  affordance; `user_stopped` shows the chip without one. */
  protected interruptedReasonFor(messageId: string): 'user_stopped' | 'connection_lost' | null {
    if (
      !this.chatStateService.lastTurnInterrupted() ||
      this.isChatLoading() ||
      messageId !== this.lastMessageId()
    ) {
      return null;
    }
    return this.chatStateService.lastTurnInterruptReason() === 'user_stopped'
      ? 'user_stopped'
      : 'connection_lost';
  }

  /** Session artifacts, newest first. Anchored ones render inline after
   *  their producing assistant message (`producedByMessageIndex` matches
   *  the `msg-{sessionId}-{index}` id); the rest fall back to the
   *  end-of-conversation strip. */
  protected artifacts = this.artifactState.artifacts;

  /** Trailing 0-based index from a `msg-{sessionId}-{index}` id. Splits
   *  on the last `-` so a session id containing dashes is irrelevant.
   *  Null for any id that doesn't end in an integer. */
  private parseMessageIndex(id: string): number | null {
    const dash = id.lastIndexOf('-');
    if (dash < 0) return null;
    const n = Number(id.slice(dash + 1));
    return Number.isInteger(n) ? n : null;
  }

  /** The message index an artifact anchors to. Live events carry a
   *  concrete producing message id (stable as later turns append);
   *  reload hydration only has the AgentCore-Memory numeric index, which
   *  is exact there. Prefer the live id, fall back to the index. */
  private resolveArtifactIndex(a: Artifact): number | null {
    if (a.producedByMessageId) {
      const live = this.parseMessageIndex(a.producedByMessageId);
      if (live !== null) return live;
    }
    return a.producedByMessageIndex ?? null;
  }

  private readonly loadedMessageIndices = computed<ReadonlySet<number>>(() => {
    const s = new Set<number>();
    for (const m of this.messages()) {
      const n = this.parseMessageIndex(m.id);
      if (n !== null) s.add(n);
    }
    return s;
  });

  /** artifacts grouped by the message index that produced them, limited
   *  to indices that are actually in the loaded (possibly paginated)
   *  message list. */
  private readonly artifactsByMessageIndex = computed<
    ReadonlyMap<number, Artifact[]>
  >(() => {
    const loaded = this.loadedMessageIndices();
    const map = new Map<number, Artifact[]>();
    for (const a of this.artifacts()) {
      const idx = this.resolveArtifactIndex(a);
      if (idx == null || !loaded.has(idx)) continue;
      const list = map.get(idx);
      if (list) list.push(a);
      else map.set(idx, [a]);
    }
    return map;
  });

  /** Artifacts with no usable anchor (legacy rows written before linkage,
   *  or an index pointing outside the loaded page) — keep them visible in
   *  the end-of-conversation strip so nothing silently disappears. */
  protected readonly orphanArtifacts = computed<Artifact[]>(() => {
    const loaded = this.loadedMessageIndices();
    return this.artifacts().filter((a) => {
      const idx = this.resolveArtifactIndex(a);
      return idx == null || !loaded.has(idx);
    });
  });

  protected readonly hasOrphanArtifacts = computed(
    () => this.orphanArtifacts().length > 0,
  );

  protected artifactsForMessageId(id: string): Artifact[] {
    const n = this.parseMessageIndex(id);
    if (n === null) return [];
    return this.artifactsByMessageIndex().get(n) ?? [];
  }

  /** Single end-of-conversation compaction summary inputs. Sourced from
   *  live SSE events plus session-metadata hydration on load. The fade-in
   *  animation only fires on live events; reload-hydrated totals appear
   *  in place. */
  protected hasCompaction = this.compactionSummary.hasCompaction;
  protected totalSummarizedTurns = this.compactionSummary.totalSummarizedTurns;
  protected animateCompaction = computed(
    () => this.compactionSummary.hasCompaction() && !this.compactionSummary.wasHydrated(),
  );

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

  /** Messages grouped into turns: each user message starts a group and the
   *  assistant messages that follow it belong to that group. Keyed by the
   *  first message's id so a group's identity (and its DOM subtree — which
   *  can hold live MCP App iframes) is stable for the conversation's
   *  lifetime: when a new turn starts, prior groups are untouched; only the
   *  min-height binding on the previously-last group flips off. A leading
   *  assistant message with no preceding user message (pagination cutting
   *  mid-turn) forms a headless first group. */
  protected readonly turns = computed<{ key: string; messages: Message[] }[]>(() => {
    const groups: { key: string; messages: Message[] }[] = [];
    for (const m of this.messages()) {
      if (m.role === 'user' || groups.length === 0) {
        groups.push({ key: m.id, messages: [m] });
      } else {
        groups[groups.length - 1].messages.push(m);
      }
    }
    return groups;
  });

  /**
   * Min-height for the LAST turn group, replacing the old fixed
   * viewport-sized bottom spacer. Reserving the space around the turn
   * instead of after it means the assistant response streams into the
   * reserved space (no scroll-height growth mid-stream) and a response
   * taller than the viewport leaves zero dead space below it — the
   * scrollable extent past the last user message is always exactly what
   * scrollToMessage needs to pin it at the top, and no more.
   *
   * Pure CSS (no measurement), so the full scroll height exists in the
   * same layout pass as the messages — the navigation scroll restore in
   * ConversationPage relies on that.
   *
   * Full-page mode scrolls the app shell's overflow container, which is
   * exactly 100dvh tall (main.h-dvh in app.html): 100dvh minus the
   * scroll-mt-20 anchor offset (HEADER_HEIGHT + SCROLL_PADDING) minus the
   * 7.5rem padding-bottom of .chat-messages-container.full-page minus the
   * shell wrapper's py-10 bottom padding (2.5rem) — both paddings already
   * contribute scrollable space below the turn.
   *
   * Embedded mode scrolls .chat-messages-container.embedded, which is a
   * size query container: 100cqh is its content-box height; its 5rem
   * padding-bottom counts toward the scroll extent but its 1rem
   * padding-top sits above the scrollIntoView target, hence +1rem net.
   * Where no query container exists (shared view), cqh falls back to
   * small-viewport units, which errs slightly roomy — harmless there.
   */
  protected readonly lastTurnMinHeight = computed<string>(() =>
    this.embeddedMode()
      ? 'calc(100cqh + 1rem)'
      : `calc(100dvh - ${this.HEADER_HEIGHT + this.SCROLL_PADDING + 120 + 40}px)`
  );

  /**
   * Scrolls to a specific message by ID
   * Call this explicitly when user submits a message
   *
   * scrollIntoView works against whichever ancestor actually scrolls — the
   * app shell's overflow container in full-page mode (the window itself no
   * longer scrolls since the shell became a real scroll container for the
   * sticky frosted nav), the embedded chat scrollport in embedded mode.
   * The fixed-header offset in full-page mode comes from scroll-mt-20 on
   * the user-message wrapper, not from math here.
   *
   * @param behavior 'smooth' for user-visible animation (submit affordance),
   *   'auto' for instant positioning (navigation restore — animating a jump
   *   across a whole conversation would be noise).
   */
  scrollToMessage(messageId: string, behavior: ScrollBehavior = 'smooth'): void {
    if (!this.isBrowser) return;

    const element = document.getElementById(`message-${messageId}`);
    if (!element) return;

    element.scrollIntoView({ behavior, block: 'start' });
  }

  /**
   * Scrolls to the last user message
   */
  scrollToLastUserMessage(behavior: ScrollBehavior = 'smooth'): void {
    const msgs = this.messages();
    const lastUserMsg = [...msgs].reverse().find(m => m.role === 'user');
    if (lastUserMsg) {
      this.scrollToMessage(lastUserMsg.id, behavior);
    }
  }
}

