import { Injectable, inject, signal } from '@angular/core';
import { ChatStateService } from './chat-state.service';

interface FailedSend {
  sessionId: string;
  retry: () => void;
}

/**
 * Retry handles for user messages whose `/chat/stream` was refused before any
 * of the response streamed (`Message.sendFailure`).
 *
 * The bubble that shows "Not sent" needs a way to resend, and the resend has
 * to go through `ChatRequestService` — which a leaf component should not
 * inject. So the request service registers a closure here when a send fails,
 * and the bubble calls it by message id. Same direction-of-dependency trick as
 * the consent services' `setResumeHandler`.
 */
@Injectable({ providedIn: 'root' })
export class FailedSendService {
  private readonly chatState = inject(ChatStateService);
  private readonly sends = signal<ReadonlyMap<string, FailedSend>>(new Map());

  register(messageId: string, sessionId: string, retry: () => void): void {
    this.sends.update(sends => new Map(sends).set(messageId, { sessionId, retry }));
  }

  /**
   * Whether a retry would start a turn right now. False while that
   * conversation is still streaming another response, where a resend would
   * only be refused by the single-flight guard again.
   */
  canRetry(messageId: string): boolean {
    const send = this.sends().get(messageId);
    return !!send && !this.chatState.isSessionLoading(send.sessionId);
  }

  retry(messageId: string): void {
    const send = this.sends().get(messageId);
    if (!send || this.chatState.isSessionLoading(send.sessionId)) return;
    this.sends.update(sends => {
      const next = new Map(sends);
      next.delete(messageId);
      return next;
    });
    send.retry();
  }
}
