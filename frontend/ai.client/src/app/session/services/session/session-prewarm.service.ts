import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, Injector, PLATFORM_ID, inject } from '@angular/core';
import { v4 as uuidv4 } from 'uuid';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { TurnSelectionService } from '../chat/turn-selection.service';
import { SessionAffinityService } from './session-affinity.service';

/**
 * The selection keys a prepare may carry, from the turn's shared selection
 * (`TurnSelectionService.build`). An allowlist: nothing of the message, its
 * files, folders, deliveries, handoff fields or steering can ride a prewarm.
 */
export const PREPARE_SELECTION_KEYS = [
  'model_id',
  'temperature',
  'caching_enabled',
  'enabled_tools',
  'provider',
  'max_tokens',
  'inference_params',
  'rag_assistant_id',
  'agent_type',
  'enabled_skills',
  'selected_prompt_id',
  'incognito',
  'agent_mention',
  'system_prompt',
] as const;

/**
 * What the page knows that the selection depends on, read when the user first
 * types. Null means the page cannot tell yet (an Agent or project binding is
 * still resolving), and the prepare is skipped.
 */
export interface PrepareContext {
  assistantId?: string;
}

/** What the composer knows at the first input. */
export interface TypingContext {
  /** An `@`-mentioned Agent is in the composer: the send's routing is not knowable here. */
  mention?: boolean;
}

/**
 * Starts a conversation's AgentCore Runtime session before its next send.
 *
 * The Runtime starts a conversation's microVM only when an invocation arrives
 * for it, so the first send used to pay that start (session creation and a V2
 * snapshot restore, about 1.5–2 s). This service asks app-api
 * (`POST /chat/prewarm`) to send a no-op warm invocation pinned to the same
 * microVM the next turn will use, so the start overlaps the user reading and
 * typing. See docs/specs/agentcore-runtime-v2.md §5a.
 *
 * Two triggers, matched to how strong each page's intent to send is:
 * - **A new conversation warms on page load** ({@link warmNewConversation} /
 *   {@link warm}). Opening it is almost always intent to send, and it is where
 *   paste-then-send and prompt-starter sends happen, which a keystroke trigger
 *   would miss or warm too late. Only while the tab is visible; a background
 *   tab warms when the user first looks at it, and again on return after
 *   {@link REWARM_AFTER_MS}.
 * - **An existing conversation warms on the composer's first input**
 *   ({@link armOnTyping} then {@link noteTyping}). Opening one is often just
 *   reading, and a reply takes long enough to type that the warm still
 *   finishes first.
 *
 * **Prepare** (`features.sessionPrewarmPrepare`): the warm made at the
 * composer's first input also carries `prepare: true` and the selection the
 * next send would use ({@link PREPARE_SELECTION_KEYS}, built by
 * `TurnSelectionService`, the same builder the send calls), so the backend can
 * build that turn's agent while the user types. An existing conversation sends
 * the prepare instead of the plain warm (it implies one); a new conversation
 * keeps its page-load warm and adds a prepare on its first input. Prepare is
 * its own limiter stage: a plain warm never suppresses it, a second prepare
 * inside the window is suppressed, and a real turn counts as both. The prepare
 * waits for the tool and skill lists off the input handler, and sends a plain
 * warm instead when it cannot tell what the send would use.
 *
 * Common to both:
 * - At most once per conversation per {@link REWARM_AFTER_MS}, inside the
 *   Runtime's 900 s idle timeout; a real turn counts ({@link noteTurn}).
 *   app-api applies the same window and a per-user rate limit.
 * - Fire-and-forget, errors swallowed without a toast. A send never waits for
 *   a warm call here. One that lands mid-warm is refused 409 by the Runtime
 *   while the session starts; app-api's chat proxy waits that out and resends
 *   it (`_wait_out_runtime_session_start`), so the SPA sees one send.
 * - A new conversation's id is minted here and handed to its first send
 *   through {@link claimNewConversationId}, so the turn lands on the warmed
 *   microVM. While the flag is off nothing is minted or sent.
 */
@Injectable({ providedIn: 'root' })
export class SessionPrewarmService {
  /** Re-warm the same conversation no sooner than this (app-api uses the same 10 minutes). */
  static readonly REWARM_AFTER_MS = 10 * 60 * 1000;

  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly affinity = inject(SessionAffinityService);

  private affinityBody(sessionId: string): { affinity_session_id?: string } {
    const root = this.affinity.affinityFor(sessionId);
    return root ? { affinity_session_id: root } : {};
  }
  private readonly injector = inject(Injector);
  private selectionService: TurnSelectionService | null = null;
  /** Resolved on first use: only a prepare needs the model, tool and skill services behind it. */
  private get selection(): TurnSelectionService {
    return (this.selectionService ??= this.injector.get(TurnSelectionService));
  }
  private readonly features = inject(FEATURES);
  private readonly document = inject(DOCUMENT);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  /** The plain-warm stage: a warm, a prepare (which implies one) or a real turn. */
  private readonly lastWarmedAt = new Map<string, number>();
  /** The prepare stage: a prepare or a real turn. A plain warm never sets it. */
  private readonly lastPreparedAt = new Map<string, number>();
  /** Real warm calls only (not turns), for the turn-timing report: the earlier stage's. */
  private readonly prewarmedAt = new Map<string, number>();
  /** When the prepare request was sent, for the turn-timing report. */
  private readonly preparedAt = new Map<string, number>();
  /** The A/B arm app-api reported for the conversation's prepare; absent means no arm. */
  private readonly prepareArms = new Map<string, 'treated' | 'holdout'>();
  /** Bumped by each real turn, so a response that lands after a send began is not attributed to it. */
  private readonly turnEpoch = new Map<string, number>();
  /** Prepares waiting on their sources; a real turn drops its entry. */
  private readonly preparing = new Set<string>();
  /** The conversation whose first input sends a prepare, with the page's resolver for it. */
  private prepareId: string | null = null;
  private prepareContext: (() => PrepareContext | null) | null = null;
  private pendingNewConversationId: string | null = null;
  /** The new conversation the visible page shows; re-warmed when the tab comes back. */
  private currentId: string | null = null;
  /** The existing conversation the page shows, warmed on the composer's first input. */
  private armedId: string | null = null;
  private listening = false;

  /** Whether this build also prepares the turn's agent at the first input. */
  private get prepares(): boolean {
    return this.enabled && this.features.sessionPrewarmPrepare === true;
  }

  /**
   * The conversation page tells the service how to read what a send from it
   * would use (its Agent binding). Registered once; a composer outside the
   * conversation page registers nothing and never prepares.
   */
  setPrepareContext(resolver: (() => PrepareContext | null) | null): void {
    this.prepareContext = resolver;
  }

  /** Whether this build warms sessions at all. */
  get enabled(): boolean {
    return this.features.sessionPrewarm && this.isBrowser;
  }

  /**
   * Warm the conversation a new-conversation page will start, minting its id.
   * The same id is returned until a send claims it.
   */
  warmNewConversation(): void {
    if (!this.enabled) {
      return;
    }
    this.pendingNewConversationId ??= uuidv4();
    this.warm(this.pendingNewConversationId);
  }

  /**
   * The id a new conversation should start with, if one was warmed; clears it.
   * Null while the flag is off or before anything was warmed, and the caller
   * mints its own.
   */
  claimNewConversationId(): string | null {
    const id = this.pendingNewConversationId;
    this.pendingNewConversationId = null;
    return id;
  }

  /** Warm a new (staged) conversation now: the page-load trigger. */
  warm(sessionId: string | null | undefined): void {
    if (!this.enabled || !sessionId) {
      return;
    }
    this.armedId = null;
    this.currentId = sessionId;
    // A new conversation keeps this page-load warm and prepares on first input.
    this.prepareId = this.prepares ? sessionId : null;
    this.listenForVisibility();
    if (this.document.visibilityState !== 'visible') {
      return; // warmed by the visibility listener when the user looks at it
    }
    this.send(sessionId);
  }

  /**
   * Arm an existing conversation to warm on the composer's first input,
   * instead of on load: opening one is often just reading.
   */
  armOnTyping(sessionId: string | null | undefined): void {
    if (!this.enabled || !sessionId) {
      return;
    }
    this.currentId = null;
    this.armedId = sessionId;
    this.prepareId = null;
  }

  /**
   * The user typed or pasted into the composer. Warms the armed conversation,
   * once per window; a no-op on any page that armed nothing, so composers
   * outside the conversation page never warm. With prepare on, the first input
   * sends a prepare instead of the plain warm (existing conversation) or after
   * the page-load warm (new conversation).
   */
  noteTyping(context: TypingContext = {}): void {
    if (!this.enabled) {
      return;
    }
    if (this.prepares && this.prepareContext) {
      const id = this.armedId ?? this.prepareId;
      if (id) {
        // A mention is not knowable from here: an existing conversation falls
        // back to the plain warm, a new one is already warm.
        if (context.mention) {
          if (this.armedId) {
            this.send(id);
            this.armedId = null;
          }
          return;
        }
        this.armedId = null;
        this.prepareId = null;
        this.prepare(id);
        return;
      }
    }
    if (this.armedId) {
      this.send(this.armedId);
    }
  }

  /**
   * Milliseconds since this tab sent a warm call for the conversation, or null
   * when it never did. Read by the turn-timing report to split warmed from
   * cold first turns; turns themselves do not count here.
   */
  warmAgeMs(sessionId: string | null | undefined): number | null {
    const at = sessionId ? this.prewarmedAt.get(sessionId) : undefined;
    return at === undefined ? null : Math.max(0, Date.now() - at);
  }

  /**
   * Milliseconds since this tab sent a prepare for the conversation, or null
   * when it sent none since the conversation's last turn. A prepare can only
   * help the first send after it, so `noteTurn` forgets it and the arm.
   */
  preparedAgeMs(sessionId: string | null | undefined): number | null {
    const at = sessionId ? this.preparedAt.get(sessionId) : undefined;
    return at === undefined ? null : Math.max(0, Date.now() - at);
  }

  /** The arm app-api reported for the prepare since the last turn, or null (no prepare, no arm, flag off). */
  prepareArm(sessionId: string | null | undefined): 'treated' | 'holdout' | null {
    return (sessionId ? this.prepareArms.get(sessionId) : undefined) ?? null;
  }

  /** A real turn started on this conversation: it warms the microVM and builds the agent. */
  noteTurn(sessionId: string | null | undefined): void {
    if (sessionId) {
      const now = Date.now();
      this.lastWarmedAt.set(sessionId, now);
      this.lastPreparedAt.set(sessionId, now);
      this.preparing.delete(sessionId);
      // This send was the one the prepare was for: later turns report neither.
      this.preparedAt.delete(sessionId);
      this.prepareArms.delete(sessionId);
      this.turnEpoch.set(sessionId, (this.turnEpoch.get(sessionId) ?? 0) + 1);
    }
  }

  /** Forget the page's conversation, so neither returning nor typing warms it. */
  clearCurrent(): void {
    this.currentId = null;
    this.armedId = null;
    this.prepareId = null;
  }

  private send(sessionId: string): void {
    const now = Date.now();
    const last = this.lastWarmedAt.get(sessionId);
    if (last !== undefined && now - last < SessionPrewarmService.REWARM_AFTER_MS) {
      return;
    }
    this.lastWarmedAt.set(sessionId, now);
    this.post(sessionId, now, {});
  }

  /**
   * The prepare stage: warm the microVM and ask the backend to build the
   * agent the next send would use. Fire-and-forget off the input handler: it
   * waits for the tool and skill lists the selection reads, and sends a plain
   * warm instead when the selection cannot be built or the page cannot tell
   * what the send would use.
   */
  private prepare(sessionId: string): void {
    const now = Date.now();
    const last = this.lastPreparedAt.get(sessionId);
    if (last !== undefined && now - last < SessionPrewarmService.REWARM_AFTER_MS) {
      return;
    }
    const alreadyWarm = this.warmWithinWindow(sessionId, now);
    this.lastPreparedAt.set(sessionId, now);
    this.lastWarmedAt.set(sessionId, now);
    this.preparing.add(sessionId);
    void this.sendPrepare(sessionId, alreadyWarm);
  }

  private async sendPrepare(sessionId: string, alreadyWarm: boolean): Promise<void> {
    let selection: Record<string, unknown> | null = null;
    try {
      selection = await this.prepareSelection(sessionId);
    } catch {
      selection = null;
    }
    // A real turn started meanwhile, which warmed and built the agent itself.
    if (!this.preparing.delete(sessionId)) {
      return;
    }
    const now = Date.now();
    if (!selection) {
      // A plain warm is always acceptable. A conversation already warmed
      // (the new-conversation page-load warm) needs nothing more.
      if (!alreadyWarm) {
        this.post(sessionId, now, {});
      }
      return;
    }
    this.preparedAt.set(sessionId, now);
    this.post(sessionId, now, { prepare: true, ...selection });
  }

  /** The allowlisted selection a send from this page would carry, or null when it cannot be known. */
  private async prepareSelection(sessionId: string): Promise<Record<string, unknown> | null> {
    const page = this.prepareContext?.() ?? null;
    if (!page) {
      return null;
    }
    // The new-chat composer's incognito toggle is claimed only at the send.
    if (this.selection.incognitoPending()) {
      return null;
    }
    await this.selection.awaitSources();
    // A bounded wait that ran out leaves a list unloaded, and a selection
    // built from it would disclose no tools or skills: skip instead.
    if (!this.selection.sourcesLoaded()) {
      return null;
    }
    // The page may have changed what a send would use while the lists loaded.
    const settled = this.prepareContext?.() ?? null;
    if (!settled) {
      return null;
    }
    const built = this.selection.build(sessionId, { assistantId: settled.assistantId });
    const picked: Record<string, unknown> = {};
    for (const key of PREPARE_SELECTION_KEYS) {
      if (key in built) {
        picked[key] = built[key];
      }
    }
    return picked;
  }

  private warmWithinWindow(sessionId: string, now: number): boolean {
    const last = this.lastWarmedAt.get(sessionId);
    return last !== undefined && now - last < SessionPrewarmService.REWARM_AFTER_MS;
  }

  private post(sessionId: string, now: number, extra: Record<string, unknown>): void {
    // The turn-timing report's warm age is the earlier stage's: keep the
    // first request of a window, replace one from an older window.
    const earlier = this.prewarmedAt.get(sessionId);
    if (earlier === undefined || now - earlier >= SessionPrewarmService.REWARM_AFTER_MS) {
      this.prewarmedAt.set(sessionId, now);
    }
    const epoch = this.turnEpoch.get(sessionId) ?? 0;
    this.http
      .post<unknown>(
        `${this.config.appApiUrl()}/chat/prewarm`,
        // A fork warms the microVM its turns pin to: its root's.
        { session_id: sessionId, ...extra, ...this.affinityBody(sessionId) },
        { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) },
      )
      .subscribe({
        next: (response) => {
          if (extra['prepare'] === true && (this.turnEpoch.get(sessionId) ?? 0) === epoch) {
            this.rememberArm(sessionId, response);
          }
        },
        // A failed warm only means the first send starts the microVM itself.
        error: () => undefined,
      });
  }

  /** Keep the arm only when it is one of the two strings; anything else is no arm. */
  private rememberArm(sessionId: string, response: unknown): void {
    const arm = (response as { prepareArm?: unknown } | null)?.prepareArm;
    if (arm === 'treated' || arm === 'holdout') {
      this.prepareArms.set(sessionId, arm);
    }
  }

  private listenForVisibility(): void {
    if (this.listening) {
      return;
    }
    this.listening = true;
    this.document.addEventListener('visibilitychange', () => {
      if (this.document.visibilityState === 'visible' && this.currentId) {
        this.send(this.currentId);
      }
    });
  }
}
