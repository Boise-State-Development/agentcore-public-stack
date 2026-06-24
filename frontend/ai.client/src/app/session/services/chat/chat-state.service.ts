import { Injectable, Signal, computed, signal } from '@angular/core';

@Injectable({
    providedIn: 'root'
})
export class ChatStateService {

    private abortController = new AbortController();
    private readonly chatLoading = signal(false);
    readonly isChatLoading: Signal<boolean> = this.chatLoading.asReadonly();

    // Bumped to ask the message list to scroll the latest user message to the
    // top of the viewport. Lets non-composer submit paths (e.g. an MCP App
    // widget's ui/message) get the same scroll affordance the composer
    // triggers in ChatContainerComponent.onMessageSubmitted.
    private readonly scrollToLastUserSignal = signal(0);
    readonly scrollToLastUserTick: Signal<number> = this.scrollToLastUserSignal.asReadonly();

    private readonly stopReason = signal<string | null>(null);
    readonly currentStopReason: Signal<string | null> = this.stopReason.asReadonly();

    // True when the most recent turn ended in a recoverable max_tokens
    // truncation. Drives the "Continue" affordance on the last assistant
    // message. Live-only (not hydrated on reload); set from the stream_error
    // event and cleared the moment a new turn starts.
    private readonly lastTurnContinuableSignal = signal(false);
    readonly lastTurnContinuable: Signal<boolean> = this.lastTurnContinuableSignal.asReadonly();

    // ----- Session-level cost / context aggregates ---------------------------
    // Drive the cost badge above the composer. Seeded from session metadata
    // on route change, then incrementally updated via the SSE metadata event
    // each turn (addTurnCost / setContext).
    private readonly costDollarsSignal = signal(0);
    readonly costDollars: Signal<number> = this.costDollarsSignal.asReadonly();

    private readonly contextTokensSignal = signal(0);
    readonly contextTokens: Signal<number> = this.contextTokensSignal.asReadonly();

    private readonly contextWindowSignal = signal(0);
    readonly contextWindowSize: Signal<number> = this.contextWindowSignal.asReadonly();

    readonly contextPct = computed(() => {
        const window = this.contextWindowSignal();
        const tokens = this.contextTokensSignal();
        if (!window || window <= 0) return 0;
        return (tokens / window) * 100;
    });


    /**
     * Sets the chat loading state
     * @param loading - Whether the chat is currently loading
     */
    setChatLoading(loading: boolean): void {
        this.chatLoading.set(loading);
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
     * Sets the stop reason for the current message
     * @param reason - The stop reason string, or null to clear
     */
    setStopReason(reason: string | null): void {
        this.stopReason.set(reason);
    }

    /**
     * Marks (or clears) whether the last turn ended in a recoverable
     * max_tokens truncation that the user can continue from.
     */
    setLastTurnContinuable(continuable: boolean): void {
        this.lastTurnContinuableSignal.set(continuable);
    }

    /**
     * Seed the session-level cost/context signals from a session metadata
     * payload (e.g. when navigating to an existing session). Called BEFORE
     * the new metadata loads on route change to clear stale state from the
     * previous session.
     */
    seedSessionAggregates(values: {
        totalCost?: number;
        lastContextTokens?: number;
        contextWindow?: number;
    } = {}): void {
        this.costDollarsSignal.set(values.totalCost ?? 0);
        this.contextTokensSignal.set(values.lastContextTokens ?? 0);
        this.contextWindowSignal.set(values.contextWindow ?? 0);
    }

    /** Add the cost of a completed turn to the running session total. */
    addTurnCost(amount: number): void {
        if (!Number.isFinite(amount) || amount <= 0) return;
        this.costDollarsSignal.update(prev => prev + amount);
    }

    /** Set the most-recent-turn context tokens (and optionally the window). */
    setContext(tokens: number, window?: number): void {
        if (Number.isFinite(tokens) && tokens >= 0) {
            this.contextTokensSignal.set(tokens);
        }
        if (window !== undefined && Number.isFinite(window) && window > 0) {
            this.contextWindowSignal.set(window);
        }
    }

    /**
     * Resets all state to initial values
     */
    resetState(): void {
        this.chatLoading.set(false);
        this.stopReason.set(null);
        this.lastTurnContinuableSignal.set(false);
        this.costDollarsSignal.set(0);
        this.contextTokensSignal.set(0);
        this.contextWindowSignal.set(0);
    }

    // Abort controller management
    getAbortController(): AbortController {
        return this.abortController;
    }

    createNewAbortController(): AbortController {
        this.abortController = new AbortController();
        return this.abortController;
    }

    abortCurrentRequest(): void {
        this.abortController.abort();
        this.abortController = new AbortController();
    }
}

