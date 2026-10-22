import { Injectable, signal } from '@angular/core';

/** A request to put text into the composer for the user to edit and send. */
export interface ComposerDraft {
  /** The session the draft belongs to; `null` for the new-conversation composer. */
  sessionId: string | null;
  text: string;
  /** Distinguishes two identical requests so the second is not ignored. */
  nonce: number;
}

/**
 * The one way a feature hands text to the composer without reaching into
 * the component (there are several `app-chat-input` placements, so a
 * view-child chain would have to thread through all of them).
 *
 * The composer consumes a draft matching its session: it sets the textarea,
 * focuses it, and clears the request. Nothing is sent — the user edits and
 * submits as usual. First consumer: the feedback retry-with-correction
 * (docs/specs/response-feedback.md §7).
 */
@Injectable({ providedIn: 'root' })
export class ComposerDraftService {
  private nonce = 0;
  readonly pending = signal<ComposerDraft | null>(null);

  request(sessionId: string | null, text: string): void {
    this.pending.set({ sessionId, text, nonce: ++this.nonce });
  }

  /** Take the pending draft for `sessionId`, if any, clearing it. */
  consume(sessionId: string | null): ComposerDraft | null {
    const draft = this.pending();
    if (!draft || draft.sessionId !== sessionId) return null;
    this.pending.set(null);
    return draft;
  }
}
