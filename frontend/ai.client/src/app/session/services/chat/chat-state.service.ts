import { Injectable, Signal, computed, signal } from '@angular/core';

@Injectable({
    providedIn: 'root'
})
export class ChatStateService {

    private abortController = new AbortController();
    private readonly chatLoading = signal(false);
    readonly isChatLoading: Signal<boolean> = this.chatLoading.asReadonly();

    private readonly stopReason = signal<string | null>(null);
    readonly currentStopReason: Signal<string | null> = this.stopReason.asReadonly();

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
     * Sets the stop reason for the current message
     * @param reason - The stop reason string, or null to clear
     */
    setStopReason(reason: string | null): void {
        this.stopReason.set(reason);
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

