import { Injectable, Signal, signal } from '@angular/core';

@Injectable({
    providedIn: 'root'
})
export class ChatStateService {

    private abortController = new AbortController();
    private readonly chatLoading = signal(false);
    readonly isChatLoading: Signal<boolean> = this.chatLoading.asReadonly();
    
    private readonly stopReason = signal<string | null>(null);
    readonly currentStopReason: Signal<string | null> = this.stopReason.asReadonly();


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
     * Resets all state to initial values
     */
    resetState(): void {
        this.chatLoading.set(false);
        this.stopReason.set(null);
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

