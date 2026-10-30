import { HttpErrorResponse } from '@angular/common/http';
import { Injectable, Injector, inject, signal } from '@angular/core';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { Message } from '../models/message.model';
import { ComposerDraftService } from './composer-draft.service';
import { parseMessageRef } from './message-feedback.service';
import { MessageMapService } from './message-map.service';
import { SessionService } from './session.service';

/**
 * "Rewind to here" on a user message (docs/specs/conversation-rewind-and-fork.md §4).
 *
 * The server hides the message and everything after it, nothing is deleted,
 * and this puts the message's text back in the composer to edit and resend.
 * The thread keeps its id, URL, title and place in the sidebar. The next turn
 * resends the same history the earlier turns sent, so the agent reads it from
 * the prompt cache instead of paying to write it again.
 *
 * A root service the bubble calls directly, like `FailedSendService`, because
 * the action needs the message map, the composer and the session API, none of
 * which a leaf component should reach. Those are resolved when a rewind runs,
 * not when the service is built: every user bubble injects this, and a bubble
 * should not drag the HTTP stack into existence just to decide whether to
 * draw a button.
 */
@Injectable({ providedIn: 'root' })
export class ConversationRewindService {
  private readonly features = inject(FEATURES);
  private readonly chatState = inject(ChatStateService);
  private readonly injector = inject(Injector);

  /** The message a rewind is in flight for, so its button can show it and not double-fire. */
  readonly pendingMessageId = signal<string | null>(null);

  /** Whether the feature is on at all; the bubble renders nothing without it. */
  get enabled(): boolean {
    return this.features.sessionRewind;
  }

  /**
   * Whether `message` can be rewound to right now. Never while its
   * conversation streams (the server's single-flight lease would refuse it),
   * and never for a message the server has not numbered (a send that failed).
   */
  canRewind(message: Message): boolean {
    if (!this.enabled || message.role !== 'user' || message.steering || message.sendFailure) return false;
    const ref = parseMessageRef(message.id);
    if (!ref) return false;
    return !this.chatState.isSessionLoading(ref.sessionId) && this.pendingMessageId() === null;
  }

  async rewind(message: Message): Promise<boolean> {
    if (!this.canRewind(message)) return false;
    const ref = parseMessageRef(message.id)!;
    this.pendingMessageId.set(message.id);
    try {
      const result = await this.injector.get(SessionService).rewindSession(ref.sessionId, message.id);
      this.injector.get(MessageMapService).rewindToMessage(ref.sessionId, message.id, result.nextMessageIndex);
      // The hidden turn took its affordances with it: the server cleared the
      // markers, and a "Continue" or "interrupted" chip here would point at a
      // reply that is no longer on screen.
      this.chatState.setLastTurnContinuable(ref.sessionId, false);
      this.chatState.setLastTurnInterrupted(ref.sessionId, false);
      this.injector.get(ComposerDraftService).request(ref.sessionId, result.prefillText);
      return true;
    } catch (error) {
      this.injector.get(ToastService).error("Couldn't rewind", rewindErrorMessage(error));
      return false;
    } finally {
      this.pendingMessageId.set(null);
    }
  }
}

/** The server's own explanation for a refusal, which is written for the user. */
export function rewindErrorMessage(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    const detail = (error.error as { detail?: unknown } | null)?.detail;
    if (typeof detail === 'string' && detail && error.status !== 500) return detail;
  }
  return 'Something went wrong. Try again in a moment.';
}
