import { Injectable, Injector, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { UserService } from '../../../auth/user.service';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { Message } from '../models/message.model';
import { ComposerDraftStorageService } from './composer-draft-storage.service';
import { rewindErrorMessage } from './conversation-rewind.service';
import { parseMessageRef } from './message-feedback.service';
import { SessionService } from './session.service';

/**
 * "Fork from here" on a user message (docs/specs/conversation-rewind-and-fork.md §7).
 *
 * Starts a new conversation holding everything before the message, and opens
 * it with the message's text in the composer. The original stays as it is.
 * The fork's turns pin to the original's microVM (SessionAffinityService), so
 * its first turn starts from the exact history the original sent and reads
 * the shared prefix from the prompt cache.
 *
 * The text is parked as the new conversation's saved draft before navigating
 * rather than handed to the composer afterwards: the composer instance is
 * reused across conversations and adopts a conversation's saved draft when it
 * switches to it, so the text is simply there on arrival, whatever order the
 * route change and the composer's effects run in.
 */
@Injectable({ providedIn: 'root' })
export class ConversationForkService {
  private readonly features = inject(FEATURES);
  private readonly chatState = inject(ChatStateService);
  private readonly injector = inject(Injector);

  /** The message a fork is in flight for. */
  readonly pendingMessageId = signal<string | null>(null);

  get enabled(): boolean {
    return this.features.sessionFork;
  }

  /** Same rules as rewind: a numbered user message, while its conversation is idle. */
  canFork(message: Message): boolean {
    if (!this.enabled || message.role !== 'user' || message.steering || message.sendFailure) return false;
    const ref = parseMessageRef(message.id);
    if (!ref) return false;
    return !this.chatState.isSessionLoading(ref.sessionId) && this.pendingMessageId() === null;
  }

  async fork(message: Message): Promise<string | null> {
    if (!this.canFork(message)) return null;
    const ref = parseMessageRef(message.id)!;
    this.pendingMessageId.set(message.id);
    try {
      const sessions = this.injector.get(SessionService);
      const result = await sessions.forkSession(ref.sessionId, message.id);
      this.injector.get(ComposerDraftStorageService).write(result.sessionId, { text: result.prefillText });
      const userId = this.injector.get(UserService).currentUser()?.user_id ?? 'anonymous';
      sessions.addSessionToCache(result.sessionId, userId, result.title);
      await this.injector.get(Router).navigate(['/s', result.sessionId]);
      return result.sessionId;
    } catch (error) {
      this.injector.get(ToastService).error("Couldn't fork", rewindErrorMessage(error));
      return null;
    } finally {
      this.pendingMessageId.set(null);
    }
  }
}
