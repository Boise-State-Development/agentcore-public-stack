import { Component, computed, effect, input, output, inject, signal, untracked, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser, NgTemplateOutlet } from '@angular/common';
import { Message, ToolUseData } from '../../services/models/message.model';
import type { Artifact } from '../../services/artifacts/artifact.model';
import { UserMessageComponent } from './components/user-message.component';
import { AssistantMessageComponent } from './components/assistant-message.component';
import { MessageMetadataBadgesComponent } from './components/message-metadata-badges.component';
import { MessageActionsComponent } from './components/message-actions.component';
import { CitationDisplayComponent } from '../citation-display/citation-display.component';
import { PulsatingLoaderComponent } from '../../../components/pulsating-loader.component';
import { OAuthConsentPromptComponent } from './components/oauth-consent-prompt/oauth-consent-prompt.component';
import { ToolApprovalPromptComponent } from './components/tool-approval-prompt/tool-approval-prompt.component';
import { UserQuestionPromptComponent } from './components/user-question-prompt/user-question-prompt.component';
import { BrowserLoginPromptComponent } from './components/browser-login-prompt/browser-login-prompt.component';
import { CompactionSummaryComponent } from './components/compaction-summary/compaction-summary.component';
import { ArtifactCardComponent } from './components/artifact/artifact-card.component';
import { ArtifactPanelComponent } from './components/artifact/artifact-panel.component';
import { FilePreviewPanelComponent } from './components/file-preview/file-preview-panel.component';
import { ArtifactStateService } from '../../services/artifacts/artifact-state.service';
import { SharedArtifactCardComponent } from '../../../shared/artifact/shared-artifact-card.component';
import type { SharedConversationArtifact } from '../../services/share/share.service';
import { McpAppActionsComponent } from './components/mcp-app-actions/mcp-app-actions.component';
import { McpAppStateService } from '../../services/mcp-apps/mcp-app-state.service';
import { AgentFeedbackLinkComponent } from '../../../agents/components/agent-feedback-link.component';
import {
  McpAppCardStateService,
  type McpAppCard,
} from '../../services/mcp-apps/mcp-app-card-state.service';
import {
  OAuthConsentRequest,
  OAuthConsentService,
} from '../../../services/oauth-consent/oauth-consent.service';
import {
  ToolApprovalRequest,
  ToolApprovalService,
} from '../../../services/tool-approval/tool-approval.service';
import {
  UserQuestionRequest,
  UserQuestionService,
} from '../../../services/user-question/user-question.service';
import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../services/browser-login/browser-login.service';
import { CompactionSummaryService } from '../../services/chat/compaction-summary.service';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { StreamParserService } from '../../services/chat/stream-parser.service';

/**
 * One renderable unit inside a turn: the user's message, or an uninterrupted
 * run of assistant messages.
 *
 * The run is the important half. The agent loop starts a new Bedrock message
 * at every tool round trip, so a four-tool answer arrives as five assistant
 * messages. Rendered one card each — with a copy button and metadata row
 * apiece — a single answer became a column of near-empty boxes, and no tool
 * rail could ever group two calls because they were never in the same
 * message. Grouping them into a run gives one card, one set of actions, and
 * one block stream for `AssistantMessageComponent` to collapse.
 *
 * A mid-turn steer (a user message inside a streaming turn) deliberately
 * BREAKS a run: the user interjected, and the words on either side of that
 * interjection are answers to different things.
 */
interface TurnSegment {
  key: string;
  kind: 'user' | 'assistant';
  messages: Message[];
  /** Last message of the run — what per-turn affordances anchor to. */
  last: Message;
}

interface Turn {
  key: string;
  messages: Message[];
  segments: TurnSegment[];
}

/**
 * Whether a message is Bedrock protocol scaffolding rather than something a
 * person said.
 *
 * Tool results come back as USER-role messages carrying nothing but
 * `toolResult` blocks. They render at zero height — invisible to the reader —
 * but they sit between every pair of assistant messages in a tool-using turn.
 * Treated as real user messages they break the assistant run at every single
 * tool call, which is precisely the grouping this component is trying to do,
 * and they start a spurious turn group on top of that.
 *
 * A mid-turn steer is the case this must NOT catch: that is a real user
 * message with real text, and the words on either side of it are answers to
 * different things, so it genuinely should break the run.
 */
function isProtocolScaffolding(message: Message): boolean {
  if (message.role !== 'user') return false;
  return message.content.every((block) => block.type === 'toolResult');
}

function segmentTurn(messages: readonly Message[]): TurnSegment[] {
  const segments: TurnSegment[] = [];
  for (const message of messages) {
    // Invisible scaffolding must not break the run it sits inside.
    if (isProtocolScaffolding(message)) continue;

    const kind = message.role === 'user' ? 'user' : 'assistant';
    const open = segments[segments.length - 1];
    if (kind === 'assistant' && open?.kind === 'assistant') {
      open.messages.push(message);
      open.last = message;
      continue;
    }
    segments.push({ key: message.id, kind, messages: [message], last: message });
  }
  return segments;
}

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
    UserQuestionPromptComponent,
    BrowserLoginPromptComponent,
    CompactionSummaryComponent,
    ArtifactCardComponent,
    ArtifactPanelComponent,
    FilePreviewPanelComponent,
    SharedArtifactCardComponent,
    McpAppActionsComponent,
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

  /**
   * Silence thresholds for the "still working" notice.
   *
   * A model call that produces nothing looks exactly like a hung one. In prod
   * session 5f34d2b0 two turns went ~95 seconds with no output and the user
   * abandoned both — the second one while the request was, as far as the
   * telemetry shows, still in flight.
   *
   * 30s is comfortably past a normal first token (~5-7s) and past most tool
   * calls, so a healthy turn rarely trips it. 90s is past anything routine and
   * is where the phrasing stops reassuring and starts admitting something is
   * wrong. The tick is coarse because the thresholds are: the notice appears
   * within one tick of crossing them.
   */
  private readonly STALL_NOTICE_MS = 30_000;
  private readonly LONG_STALL_NOTICE_MS = 90_000;
  private readonly STALL_TICK_MS = 5_000;

  /**
   * How long `preparing` must stay the current phase before it is rendered.
   *
   * The backend announces the agent build unconditionally, because it cannot
   * time it: `create_agent` is synchronous, so a cold build occupies the
   * runtime's event loop for its whole duration and a server-side timer never
   * fires (verified on dev — a 1548ms build emitted nothing through a 250ms
   * race). The client's clock is not blocked by any of that, so the decision
   * lives here.
   *
   * 250ms is below the measured cold build (1478ms) and far above the warm one
   * (0-38ms), so a warm turn's `preparing` is superseded by `thinking` long
   * before this elapses and never reaches the screen. Which is the point: a
   * 38ms flash of "Getting ready" is unreadable, and it lands AFTER the generic
   * "Thinking" shown from the moment the user hits send, so it reads as going
   * backwards.
   */
  private readonly PREPARING_RENDER_DELAY_MS = 250;

  /** True once `preparing` has been the current phase for long enough to show. */
  private readonly preparingSettled = signal(false);

  /** Clock for the stall thresholds; only ticks while a response is pending. */
  private readonly nowMs = signal(Date.now());

  messages = input.required<Message[]>();
  isChatLoading = input<boolean>(false);
  streamingMessageId = input<string | null>(null);
  embeddedMode = input<boolean>(false);
  /**
   * Artifacts pinned into a shared conversation's snapshot, plus the
   * share that grants them — set only by the shared view.
   *
   * An input rather than a second read of `ArtifactStateService`,
   * because that service is the *owner's* session state: it is
   * populated by live SSE events and owner-scoped hydration, neither of
   * which a recipient has. Feeding it recipient rows would put another
   * user's artifacts into the signal the real session view reads.
   *
   * When set, these render in place of the owner cards. Null is the
   * normal session view.
   */
  sharedArtifacts = input<SharedConversationArtifact[] | null>(null);
  sharedArtifactShareId = input<string | null>(null);

  /**
   * The published marketplace agent behind this conversation, when there is one — the
   * foot-of-conversation feedback link, and nothing else. Null for plain chat, a legacy
   * assistant, or an agent that is not published (D15.3).
   */
  feedbackAgent = input<{ id: string; name: string } | null>(null);

  /** The conversation itself, offered to attach to that feedback. */
  sessionId = input<string | null>(null);
  /** #111: whether the active agent permits source-document download from citations.
   *  Passed straight to the citation card to hide its download button. Default true. */
  allowDownload = input<boolean>(true);
  /** Bubbled up when the user clicks "Continue" on a max_tokens-truncated
   *  assistant message. The page reuses the normal submit path with a
   *  canned prompt. */
  continueRequested = output<void>();

  private consentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private userQuestionService = inject(UserQuestionService);
  private browserLoginService = inject(BrowserLoginService);
  private compactionSummary = inject(CompactionSummaryService);
  private artifactState = inject(ArtifactStateService);
  private mcpAppCardState = inject(McpAppCardStateService);
  private mcpAppState = inject(McpAppStateService);
  private chatStateService = inject(ChatStateService);
  private toolInsight = inject(ToolInsightService);
  private streamParser = inject(StreamParserService);

  /**
   * Copy for the loading indicator while the backend retries a failed model
   * call. Null during a normal response, which leaves the usual cycling
   * phrases in place. Read straight off the parser by session id rather than
   * threaded through every `[isChatLoading]` binding — preview and test-drive
   * hosts have no session id and correctly get nothing.
   */
  constructor() {
    // Tick only while a response is pending: an always-on interval would wake
    // every open conversation forever to answer a question nobody is asking.
    // The write happens in the callback, not the effect body, so this never
    // re-triggers itself.
    if (this.isBrowser) {
      effect((onCleanup) => {
        if (!this.isChatLoading()) {
          return;
        }
        const timer = setInterval(() => this.nowMs.set(Date.now()), this.STALL_TICK_MS);
        onCleanup(() => clearInterval(timer));
      });

      // Hold `preparing` back until it has lasted long enough to be worth
      // reading. A one-shot timer rather than a poll: the phase either
      // survives the delay or is replaced, and re-running on every tick would
      // just be a slower way to ask the same question.
      effect((onCleanup) => {
        const sessionId = this.chatStateService.viewedSessionId();
        const phase = sessionId
          ? this.toolInsight.status(sessionId)?.phase
          : undefined;

        // Armed on the way IN, never cleared on the way out.
        //
        // Clearing it when the phase left `preparing` opened a propagation
        // window where the label computed with the phase still `preparing`
        // and the flag already false, fell through to "Thinking", and showed
        // a ~40ms step backwards — "Getting ready…" → "Thinking…" → "Waiting
        // for the model…" — on every single turn. Observed on dev 5/5 turns;
        // it is the exact reading this delay exists to prevent.
        //
        // Leaving the flag set costs nothing: the only branch that reads it
        // is unreachable unless the phase IS `preparing`, and entering that
        // phase again re-arms it below. The fast-build case is still
        // suppressed by `onCleanup` cancelling the pending timer, which is
        // what actually keeps a 38ms build off the screen.
        if (phase !== 'preparing') {
          return;
        }

        // Untracked: writing a signal this effect also reads would loop.
        untracked(() => this.preparingSettled.set(false));

        const timer = setTimeout(
          () => this.preparingSettled.set(true),
          this.PREPARING_RENDER_DELAY_MS,
        );
        onCleanup(() => clearTimeout(timer));
      });
    }
  }

  /**
   * Copy for a response that has gone quiet for long enough to look broken.
   *
   * Deliberately client-side. The server cannot say anything during a stalled
   * model call without racing the agent stream against a timer, and that
   * machinery — cancellation, lease release, interrupted-turn persistence —
   * has already been the source of several production bugs. The SPA has the
   * one fact that matters: when the last byte arrived. If the connection had
   * dropped, fetch-event-source would have surfaced an error instead of
   * silence, so silence on an open stream really is "the server has not sent
   * anything yet".
   */
  protected readonly stallNotice = computed<string | null>(() => {
    const sessionId = this.sessionId();
    if (!sessionId || !this.isChatLoading()) {
      return null;
    }
    const lastEventAt = this.streamParser.lastEventAtFor(sessionId)();
    if (!lastEventAt) {
      return null;
    }
    const silentFor = this.nowMs() - lastEventAt;
    if (silentFor >= this.LONG_STALL_NOTICE_MS) {
      return 'Still working \u2014 this is taking longer than usual.';
    }
    if (silentFor >= this.STALL_NOTICE_MS) {
      return 'Still working\u2026';
    }
    return null;
  });

  protected readonly retryNotice = computed<string | null>(() => {
    const sessionId = this.sessionId();
    if (!sessionId) {
      return null;
    }
    const retry = this.streamParser.modelRetryFor(sessionId)();
    if (!retry) {
      return null;
    }
    return retry.attempt === 1
      ? 'The model is busy. Retrying\u2026'
      : `The model is busy. Retrying \u2014 attempt ${retry.attempt}.`;
  });

  /**
   * What the loading indicator says. A retry is a specific, known fact and
   * outranks the generic stall notice, which is only ever an inference from
   * elapsed silence.
   */
  protected readonly loaderNotice = computed<string | null>(
    () => this.retryNotice() ?? this.stallNotice(),
  );

  /**
   * What the agent is doing right now, phrased for the loading indicator.
   *
   * Derived from the `agent_status` stream, so it is a fact rather than an
   * inference — the difference between "waiting on Canvas for 9 seconds" and
   * "hung" was previously invisible to the user, and both looked like
   * "Pondering...".
   *
   * Returns null once text starts streaming: at that point the loader is gone
   * anyway, and a lingering "Thinking" under a visible answer would be wrong.
   */
  protected readonly loaderStatus = computed<string | null>(() => {
    if (this.loaderStatusTool()) return 'Running';

    // `thinking` is the runtime telling us the model call is in flight, which
    // is a narrower and more useful claim than the fallback: it rules out the
    // agent build, the session restore and the head-of-turn context work that
    // all precede it and all read "Thinking" today.
    //
    // Only while the answer is still silent, though. The loader stays mounted
    // for the whole turn — `isChatLoading` clears at stream close, not at the
    // first token — so once text is arriving, "Waiting for the model" would
    // contradict what the user can already read. In that case this says
    // exactly what it said before, which is vague but not wrong.
    const sessionId = this.chatStateService.viewedSessionId();
    const phase = sessionId ? this.toolInsight.status(sessionId)?.phase : undefined;

    // The agent is being built — tool registry, MCP pre-flight, session
    // restore. The backend only sends this once the build has already proven
    // slow (measured: 1478ms on a cold agent-cache miss, 0-38ms warm), so it
    // never flickers past on the common path.
    if (phase === 'preparing') {
      // Not yet settled means the build is still plausibly a fast one, and a
      // label that appears for 38ms is a flicker rather than a status.
      //
      // `prepared` deliberately does NOT land here: it means the build is
      // over, so it falls through to the generic label below. That frame is
      // what makes the settle timer work at all — the build's END used to be
      // inferred from `thinking`, which arrives only after the head-of-turn
      // work, so a 1ms build still sat in `preparing` past the delay and
      // rendered "Getting ready…".
      return this.preparingSettled() ? 'Getting ready' : 'Thinking';
    }

    if (phase === 'thinking' && !this.hasStreamedText()) {
      return 'Waiting for the model';
    }

    return 'Thinking';
  });

  /**
   * Whether the turn's newest assistant message has produced visible text yet.
   *
   * First-hand from the content stream, which is the only place this is
   * knowable without the backend duplicating a fact the client already holds.
   */
  private readonly hasStreamedText = computed<boolean>(() => {
    const messages = this.messages();
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i];
      if (message.role !== 'assistant') continue;
      return message.content.some(block => !!block.text?.trim());
    }
    return false;
  });

  // A count of the OTHER tools in a batch used to live here, rendering
  // "Running list_assignments and 2 more". It was removed as dead code: the
  // agent pins `tool_executor=SequentialToolExecutor()`
  // (`agents/main_agent/core/agent_factory.py`) so that concurrent browser
  // tools cannot start two Playwright sessions, which means a batch emits
  // strictly interleaved start/end pairs and more than one tool is NEVER in
  // flight. Verified on dev against a real three-tool batch. If that executor
  // ever becomes concurrent, `ToolInsightService.runningTools` already tracks
  // the whole set and the count is a few lines to restore.

  /**
   * The tool currently executing, or null.
   *
   * `agent_status` first, the content stream as the fallback.
   *
   * The order used to be the other way round, for a good reason that no
   * longer holds: the coordinator drained status transitions only between
   * yields of the agent stream, and during tool execution that stream yields
   * nothing — so a `tool_start` sat in the queue for exactly the silent
   * stretch it exists to explain and arrived alongside its own `tool_end`.
   * Measured on a three-tool browse turn: the indicator read "Thinking" for
   * the entire 4.5s the tools were running and never once showed their name.
   * PR-2 drains concurrently, so the transitions now arrive while the tools
   * are running (docs/specs/agent-state-feedback.md).
   *
   * `agent_status` is preferred because it knows things the content stream
   * cannot: that a batch has THREE tools in it rather than one, and that a
   * tool has finished (a `toolUse` block whose result has not streamed in yet
   * looks identical to one still executing).
   *
   * The content-stream derivation stays as the fallback rather than being
   * deleted. It needs no round trip, so it is strictly more current when it
   * fires, and it is the only source left if the live drain is killed by its
   * flag or a future SDK change starves the queue.
   *
   * Shown verbatim rather than prettified — `list_assignments` is the thing
   * that is running, and it is the same identifier the tool rail and the
   * admin catalog use. Humanising it would invent a second name for one
   * thing.
   */
  protected readonly loaderStatusTool = computed<string | null>(() => {
    const sessionId = this.chatStateService.viewedSessionId();
    if (sessionId) {
      const running = this.toolInsight.runningTools(sessionId);
      if (running.length) return running[0].toolName;
    }

    const messages = this.messages();
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i];
      if (message.role !== 'assistant') continue;
      // Only the newest assistant message can have work in flight; an older
      // one with an unresolved tool is an abandoned turn, not a running one.
      for (const block of message.content) {
        const toolUse = block.toolUse as ToolUseData | undefined | null;
        if (toolUse?.name && !toolUse.result) return toolUse.name;
      }
      return null;
    }
    return null;
  });

  /** Epoch ms the viewed conversation's turn started, for the elapsed timer. */
  protected readonly loaderStartedAt = computed<number | null>(() => {
    const sessionId = this.chatStateService.viewedSessionId();
    if (!sessionId) return null;
    return this.toolInsight.turnStartedAt(sessionId) ?? null;
  });

  /**
   * Persisted app-initiated tool cards (PR #6) that have nowhere better to
   * go. Normally these surface behind their own App frame's header, keyed
   * by the originating tool-use id — that's the whole point of the frame's
   * actions chip. But a frame only renders once its `ui_resource` carries a
   * `sandboxOrigin` (no mcp-sandbox stack → no frame), and without this
   * fallback those cards would vanish silently. Provenance for a tool an
   * app ran against the user's account is not something to drop on the
   * floor, so orphans get a standalone box — still summarized, never a card
   * apiece.
   */
  protected orphanedMcpAppCards = computed<McpAppCard[]>(() =>
    this.mcpAppCardState
      .cards()
      .filter(
        (card) =>
          !this.mcpAppState.get(
            this.chatStateService.viewedSessionId(),
            card.toolUseId,
          )?.sandboxOrigin,
      ),
  );

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

  /**
   * The one-line recap shown at the foot of a finished turn — "9.6s · 4 tools".
   *
   * Everything else about a turn disappears when it ends: the loading line
   * goes, and with it the elapsed timer the user was watching. The tool rail
   * keeps per-tool durations, but nothing said how long the turn took.
   *
   * `turnDurationMs` rather than `latency.endToEndLatency`, which is not the
   * same number: the persisted form of that field prefers the provider's own
   * API-call time, so summing it across a turn drops tool execution and the
   * pre-stream agent build — a turn the user watched for 9s reads as 3s. The
   * backend sends `turnDurationMs` on the live stream AND persists it on the
   * turn's last message, so this reads the same either way.
   *
   * Null while the turn is still streaming (the duration only exists once it
   * has ended) and null for a turn that predates the field, which is why the
   * footer is absent on old conversations rather than showing a zero.
   */
  protected readonly lastTurnRecap = computed<string | null>(() => {
    // Strict alternation with the loading line, which is the whole point: the
    // recap renders in the loader's own slot, so exactly one of the two is on
    // screen at any moment and the finished state reads as the live state
    // settling rather than as a second, different thing appearing beneath it.
    //
    // `isChatLoading` is the signal that makes that true, because it spans the
    // WHOLE turn — it clears at stream close, not at the last token. The
    // narrower `streamingMessageId` clears at `message_stop`, which is earlier,
    // and gating on it alone put the recap on screen while the loader was still
    // running. Both are kept: the first guarantees the alternation, the second
    // still holds in any context where `isChatLoading` is not threaded through.
    if (this.isChatLoading()) return null;

    // Only the latest turn. The number describes what just happened, and a
    // column of durations down the whole conversation turns a punctuation mark
    // into a metrics readout — every earlier turn's recap is a fact nobody
    // asked for, competing with the answer it sits under.
    const turns = this.turns();
    if (!turns.length) return null;

    const assistant = turns[turns.length - 1].segments.filter(
      (s) => s.kind === 'assistant',
    );
    if (!assistant.length) return null;

    const last = assistant[assistant.length - 1].last;
    if (last.id === this.streamingMessageId()) return null;

    const durationMs = last.metadata?.['turnDurationMs'];
    if (typeof durationMs !== 'number' || durationMs <= 0) return null;

    const parts = [this.formatDuration(durationMs)];

    const tools = assistant.reduce(
      (count, segment) =>
        count +
        segment.messages.reduce(
          (n, message) =>
            n +
            message.content.filter(
              (block) => block.type === 'toolUse' || block.type === 'tool_use',
            ).length,
          0,
        ),
      0,
    );
    if (tools > 0) parts.push(`${tools} tool${tools === 1 ? '' : 's'}`);

    return parts.join(' \u00b7 ');
  });

  /** Seconds under a minute, then minutes — matching the loader's readout. */
  private formatDuration(ms: number): string {
    if (ms < 1000) return '<1s';
    const seconds = ms / 1000;
    if (seconds < 10) {
      // One decimal, but only when it says something: "4.0s" is noise where
      // "4s" is the same fact.
      const tenths = seconds.toFixed(1);
      return tenths.endsWith('.0') ? `${tenths.slice(0, -2)}s` : `${tenths}s`;
    }
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole}s`;
    return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  }

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
    // Two UI outcomes, not one per reason: only a deliberate Stop withholds
    // the Continue affordance. Every other reason — `navigated_away`,
    // `connection_lost`, `unknown` — means the user never rejected the
    // answer, so Continue is offered. New reasons bucket here by default,
    // which is the safe direction: offering Continue on a turn the user
    // abandoned is recoverable; withholding it on one they still want is not.
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

  // ----------------------------------------------------------------
  // Shared-conversation artifacts (recipient view)
  // ----------------------------------------------------------------

  /** Shared artifacts grouped by the turn that produced them. Uses only
   *  `producedByMessageIndex` — a snapshot has no live producing message
   *  id, because nothing streamed into it. */
  private readonly sharedArtifactsByMessageIndex = computed<
    ReadonlyMap<number, SharedConversationArtifact[]>
  >(() => {
    const loaded = this.loadedMessageIndices();
    const map = new Map<number, SharedConversationArtifact[]>();
    for (const a of this.sharedArtifacts() ?? []) {
      const idx = a.producedByMessageIndex;
      if (idx == null || !loaded.has(idx)) continue;
      const list = map.get(idx);
      if (list) list.push(a);
      else map.set(idx, [a]);
    }
    return map;
  });

  /** Shared artifacts with no usable anchor — kept visible in the
   *  end-of-conversation strip, for the same reason the owner's orphans
   *  are: an artifact that silently vanishes is worse than one in the
   *  wrong place. */
  protected readonly orphanSharedArtifacts = computed<
    SharedConversationArtifact[]
  >(() => {
    const loaded = this.loadedMessageIndices();
    return (this.sharedArtifacts() ?? []).filter(
      (a) =>
        a.producedByMessageIndex == null ||
        !loaded.has(a.producedByMessageIndex),
    );
  });

  protected sharedArtifactsForMessageId(
    id: string,
  ): SharedConversationArtifact[] {
    const n = this.parseMessageIndex(id);
    if (n === null) return [];
    return this.sharedArtifactsByMessageIndex().get(n) ?? [];
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
   *  not the assistant text that just streamed.
   *
   *  Filtered to THIS list's session. The service's queue is global, which was
   *  invisible while only one message list was ever mounted; the agent
   *  designer's preview and the marketplace review test drive now stream
   *  through the same interrupt protocol, so an unfiltered read would render
   *  one pane's approve/decline prompt in another pane's transcript — and
   *  resolving it there would resume a turn the reader isn't looking at.
   *  A null sessionId (a list not bound to a session) shows nothing rather
   *  than everything. */
  protected pendingToolApprovals = computed<ToolApprovalRequest[]>(() => {
    const sessionId = this.sessionId();
    if (!sessionId) return [];
    return this.toolApprovalService.pending().filter((r) => r.sessionId === sessionId);
  });

  /** Clarifying-question prompts for THIS list's session, for the same reason
   *  the approvals above are filtered: the service queue is global, and the
   *  agent-designer preview and marketplace review pane stream through the
   *  same interrupt protocol. Answering one pane's prompt in another pane's
   *  transcript would resume a turn the reader isn't looking at. */
  protected pendingUserQuestions = computed<UserQuestionRequest[]>(() => {
    const sessionId = this.sessionId();
    if (!sessionId) return [];
    return this.userQuestionService.pending().filter((r) => r.sessionId === sessionId);
  });

  /** Browser sign-in prompts for THIS list's session, filtered for the same
   *  reason as the two above: the service queue is global, and resuming a
   *  paused turn from another pane's transcript would hand the browser back on
   *  a conversation the reader isn't looking at. */
  protected pendingBrowserLogins = computed<BrowserLoginRequest[]>(() => {
    const sessionId = this.sessionId();
    if (!sessionId) return [];
    return this.browserLoginService.pending().filter((r) => r.sessionId === sessionId);
  });

  /** Messages grouped into turns: each user message starts a group and the
   *  assistant messages that follow it belong to that group. Keyed by the
   *  first message's id so a group's identity (and its DOM subtree — which
   *  can hold live MCP App iframes) is stable for the conversation's
   *  lifetime: when a new turn starts, prior groups are untouched; only the
   *  min-height binding on the previously-last group flips off. A leading
   *  assistant message with no preceding user message (pagination cutting
   *  mid-turn) forms a headless first group. */
  protected readonly turns = computed<Turn[]>(() => {
    const groups: Turn[] = [];
    for (const m of this.messages()) {
      // A mid-turn steer is a user message that does NOT start a turn: the
      // user sent it *into* the response already streaming, so it belongs to
      // that turn's group. Breaking here instead would split one response into
      // two groups and move the last group's scroll reserve out from under a
      // response still streaming into it. See docs/specs/mid-turn-steering.md.
      // Scaffolding is excluded for the same reason as steers: a tool-result
      // message is not the user starting a new turn, and treating it as one
      // split a single response into a turn group per tool call — which also
      // moved the last group's scroll reserve out from under the response
      // still streaming into it.
      const startsTurn =
        m.role === 'user' && !m.steering && !isProtocolScaffolding(m);
      if (startsTurn || groups.length === 0) {
        groups.push({ key: m.id, messages: [m], segments: [] });
      } else {
        groups[groups.length - 1].messages.push(m);
      }
    }
    for (const turn of groups) {
      turn.segments = segmentTurn(turn.messages);
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

