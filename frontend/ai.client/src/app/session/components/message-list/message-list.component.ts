import { Component, computed, input, output, signal, effect, OnDestroy, inject, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
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

  /** Bubbled up when the user clicks "Continue" on a max_tokens-truncated
   *  assistant message. The page reuses the normal submit path with a
   *  canned prompt. */
  continueRequested = output<void>();

  private consentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private compactionSummary = inject(CompactionSummaryService);
  private artifactState = inject(ArtifactStateService);
  private chatStateService = inject(ChatStateService);

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

