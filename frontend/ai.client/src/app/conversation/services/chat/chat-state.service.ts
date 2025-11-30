import { Injectable, Signal, signal } from '@angular/core';

@Injectable({
    providedIn: 'root'
})
export class ChatStateService {

    private abortController = new AbortController();
    private readonly chatLoading = signal(false);
    readonly isChatLoading: Signal<boolean> = this.chatLoading.asReadonly();


    /**
     * Sets the chat loading state
     * @param loading - Whether the chat is currently loading
     */
    setChatLoading(loading: boolean): void {
        this.chatLoading.set(loading);
    }

    /**
     * Resets all state to initial values
     */
    resetState(): void {
        this.chatLoading.set(false);
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

