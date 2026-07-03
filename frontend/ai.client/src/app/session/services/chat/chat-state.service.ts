import { Injectable, Signal, WritableSignal, computed, signal } from '@angular/core';

/**
 * Mutable chat state for one conversation. Every stream-scoped flag lives
 * here, keyed by session, so two conversations streaming concurrently can
 * never clobber each other's loading spinner, cost badge, Stop button, or
 * "Continue" affordance.
 */
interface SessionChatState {
    loading: WritableSignal<boolean>;
    stopReason: WritableSignal<string | null>;
    lastTurnContinuable: WritableSignal<boolean>;
    costDollars: WritableSignal<number>;
    contextTokens: WritableSignal<number>;
    contextWindow: WritableSignal<number>;
    /** In-flight SSE request controller, one per session. */
    abortController: AbortController | null;
}

@Injectable({
    providedIn: 'root'
})
export class ChatStateService {

    /**
     * Per-session state, held in a signal so the viewed-session facades
     * below recompute when a session's state is lazily created.
     */
    private readonly states = signal<ReadonlyMap<string, SessionChatState>>(new Map());

    /**
     * The session the user is currently looking at. Set by the session page
     * on route change (and eagerly by ChatRequestService when it creates a
     * new session, so the composer spinner doesn't wait on navigation).
     * The readonly facades below project this session's state, which keeps
     * existing consumers (composer, cost badge, Continue affordance)
     * unchanged while the underlying state is per-session.
     */
    private readonly viewedSessionIdSignal = signal<string | null>(null);
    readonly viewedSessionId: Signal<string | null> = this.viewedSessionIdSignal.asReadonly();

    readonly isChatLoading = computed(() => this.viewedState()?.loading() ?? false);
    readonly currentStopReason = computed(() => this.viewedState()?.stopReason() ?? null);
    readonly lastTurnContinuable = computed(() => this.viewedState()?.lastTurnContinuable() ?? false);

    // ----- Session-level cost / context aggregates ---------------------------
    // Drive the cost badge above the composer. Seeded from session metadata
    // on route change, then incrementally updated via the SSE metadata event
    // each turn (addTurnCost / setContext).
    readonly costDollars = computed(() => this.viewedState()?.costDollars() ?? 0);
    readonly contextTokens = computed(() => this.viewedState()?.contextTokens() ?? 0);
    readonly contextWindowSize = computed(() => this.viewedState()?.contextWindow() ?? 0);

    readonly contextPct = computed(() => {
        const window = this.contextWindowSize();
        const tokens = this.contextTokens();
        if (!window || window <= 0) return 0;
        return (tokens / window) * 100;
    });

    // Bumped to ask the message list to scroll the latest user message to the
    // top of the viewport. Lets non-composer submit paths (e.g. an MCP App
    // widget's ui/message) get the same scroll affordance the composer
    // triggers in ChatContainerComponent.onMessageSubmitted.
    private readonly scrollToLastUserSignal = signal(0);
    readonly scrollToLastUserTick: Signal<number> = this.scrollToLastUserSignal.asReadonly();

    /** Point the viewed-session facades at a (possibly null) session. */
    setViewedSession(sessionId: string | null): void {
        this.viewedSessionIdSignal.set(sessionId);
    }

    /**
     * Sets the chat loading state for a session.
     */
    setChatLoading(sessionId: string, loading: boolean): void {
        this.stateFor(sessionId).loading.set(loading);
    }

    /** Whether a specific session is currently streaming (loading). */
    isSessionLoading(sessionId: string): boolean {
        return this.states().get(sessionId)?.loading() ?? false;
    }

    /**
     * Request that the message list scroll the latest user message to the top
     * of the viewport (e.g. after a programmatic, non-composer user turn such
     * as an MCP App widget relaying a `ui/message`).
     */
    requestScrollToLastUser(): void {
        this.scrollToLastUserSignal.update(n => n + 1);
    }

    /**
     * Sets the stop reason for a session's current message.
     */
    setStopReason(sessionId: string, reason: string | null): void {
        this.stateFor(sessionId).stopReason.set(reason);
    }

    /**
     * Marks (or clears) whether a session's last turn ended in a recoverable
     * max_tokens truncation that the user can continue from.
     */
    setLastTurnContinuable(sessionId: string, continuable: boolean): void {
        this.stateFor(sessionId).lastTurnContinuable.set(continuable);
    }

    /**
     * Seed a session's cost/context signals from its metadata payload (e.g.
     * when navigating to an existing session).
     */
    seedSessionAggregates(sessionId: string, values: {
        totalCost?: number;
        lastContextTokens?: number;
        contextWindow?: number;
    } = {}): void {
        const state = this.stateFor(sessionId);
        state.costDollars.set(values.totalCost ?? 0);
        state.contextTokens.set(values.lastContextTokens ?? 0);
        state.contextWindow.set(values.contextWindow ?? 0);
    }

    /** Add the cost of a completed turn to a session's running total. */
    addTurnCost(sessionId: string, amount: number): void {
        if (!Number.isFinite(amount) || amount <= 0) return;
        this.stateFor(sessionId).costDollars.update(prev => prev + amount);
    }

    /** Set a session's most-recent-turn context tokens (and optionally the window). */
    setContext(sessionId: string, tokens: number, window?: number): void {
        const state = this.stateFor(sessionId);
        if (Number.isFinite(tokens) && tokens >= 0) {
            state.contextTokens.set(tokens);
        }
        if (window !== undefined && Number.isFinite(window) && window > 0) {
            state.contextWindow.set(window);
        }
    }

    // ----- Abort controller management ---------------------------------------

    /**
     * Create a fresh AbortController for a session's outgoing stream. Any
     * in-flight request for the SAME session is aborted first (double-submit
     * guard); streams for other sessions are untouched.
     */
    createAbortController(sessionId: string): AbortController {
        const state = this.stateFor(sessionId);
        state.abortController?.abort();
        const controller = new AbortController();
        state.abortController = controller;
        return controller;
    }

    /** Abort a session's in-flight request (Stop button), if any. */
    abortRequest(sessionId: string): void {
        const state = this.states().get(sessionId);
        state?.abortController?.abort();
        if (state) {
            state.abortController = null;
        }
    }

    private viewedState(): SessionChatState | undefined {
        const sessionId = this.viewedSessionIdSignal();
        return sessionId ? this.states().get(sessionId) : undefined;
    }

    /** Get (lazily creating) the state bucket for a session. */
    private stateFor(sessionId: string): SessionChatState {
        const existing = this.states().get(sessionId);
        if (existing) return existing;

        const created: SessionChatState = {
            loading: signal(false),
            stopReason: signal<string | null>(null),
            lastTurnContinuable: signal(false),
            costDollars: signal(0),
            contextTokens: signal(0),
            contextWindow: signal(0),
            abortController: null,
        };
        this.states.update(map => {
            const next = new Map(map);
            next.set(sessionId, created);
            return next;
        });
        return created;
    }
}
