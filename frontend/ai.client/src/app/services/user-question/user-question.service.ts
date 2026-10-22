import { Injectable, computed, signal } from '@angular/core';
import type {
  UserQuestion,
} from '../../shared/utils/stream-parser/stream-parser-types';

/**
 * A pending set of clarifying questions the agent paused its turn to ask.
 *
 * Sibling of `ToolApprovalRequest`, and the flow is identical: the agent's
 * tool call is paused (Strands interrupt), the chat UI renders an inline
 * picker, and the user's answers resume the same turn through
 * {@link UserQuestionResumeHandler}.
 *
 * One difference worth knowing: the tool-approval interrupt is raised by a
 * hook gating someone else's tool, while this one is raised by the
 * `ask_user_question` tool itself — so `toolUseId` identifies the prompt's own
 * tool card in the transcript.
 */
export interface UserQuestionRequest {
  interruptId: string;
  toolUseId: string;
  questions: UserQuestion[];
  receivedAt: number;
  /** Id of the assistant message whose tool call triggered this prompt;
   *  used by the inline message renderer to anchor the prompt. */
  messageId?: string;
  sessionId?: string;
}

/** One question's answer, keyed back to its question by `header`. */
export interface UserQuestionAnswer {
  /** Labels the user picked. Empty when they only typed free text. */
  selected: string[];
  /** Free text from the "Other" field. */
  text?: string;
}

/**
 * The payload posted back to resume the turn.
 *
 * `{ skipped: true }` is the dismissal form. It is deliberately an object and
 * never `null`: the backend's `ToolContext.interrupt` only treats a non-null
 * response as an answer, so posting null would re-raise the same interrupt
 * forever and strand the user in a paused turn.
 */
export type UserQuestionResponse =
  | { answers: Record<string, UserQuestionAnswer> }
  | { skipped: true };

/**
 * Handler the chat layer registers to resume a paused agent turn once the
 * user has answered. Receives the interrupt id and the response payload; the
 * handler POSTs to `/invocations` with the matching `interrupt_responses`
 * entry.
 */
export type UserQuestionResumeHandler = (
  interruptId: string,
  response: UserQuestionResponse,
  context?: { sessionId?: string },
) => void | Promise<void>;

/**
 * Tracks clarifying-question prompts surfaced by the SSE stream and
 * coordinates their resolution. The stream parser calls {@link requestAnswers}
 * when a `user_question_required` event arrives; components render a picker
 * bound to {@link pending}. Submitting or skipping calls {@link resolve},
 * which drops the request locally and asks the registered
 * {@link UserQuestionResumeHandler} to fire the resume request.
 */
@Injectable({ providedIn: 'root' })
export class UserQuestionService {
  private readonly requests = signal<Map<string, UserQuestionRequest>>(new Map());

  // Interrupt ids already surfaced this session, so a re-emission (stream
  // replay, network retry) doesn't resurrect a resolved prompt. Every new
  // tool call carries a fresh interrupt id, so a genuine second round of
  // questions is not suppressed by this.
  private readonly seenInterruptIds = new Set<string>();

  private resumeHandler: UserQuestionResumeHandler | null = null;

  readonly pending = computed<UserQuestionRequest[]>(() =>
    Array.from(this.requests().values()).sort(
      (a, b) => a.receivedAt - b.receivedAt,
    ),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  /**
   * Register a prompt coming off the SSE stream. Idempotent for the same
   * interruptId.
   */
  requestAnswers(input: {
    interruptId: string;
    toolUseId: string;
    questions: UserQuestion[];
    messageId?: string;
    sessionId?: string;
  }): void {
    if (this.seenInterruptIds.has(input.interruptId)) {
      return;
    }
    if (!input.questions.length) {
      return;
    }
    this.seenInterruptIds.add(input.interruptId);
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(input.interruptId, { ...input, receivedAt: Date.now() });
      return next;
    });
  }

  /**
   * Resolve a pending prompt. Drops it locally and forwards the response to
   * the registered resume handler. No-op if the request is unknown (already
   * resolved), which makes a double-submit from a double-click harmless.
   */
  async resolve(
    interruptId: string,
    response: UserQuestionResponse,
  ): Promise<void> {
    const request = this.requests().get(interruptId);
    if (!request) {
      return;
    }

    this.requests.update((map) => {
      const next = new Map(map);
      next.delete(interruptId);
      return next;
    });

    if (!this.resumeHandler) {
      console.warn(
        'UserQuestionService: no resume handler registered; answers dropped',
        { interruptId },
      );
      return;
    }

    try {
      await this.resumeHandler(interruptId, response, {
        sessionId: request.sessionId,
      });
    } catch (err) {
      console.error('UserQuestionService: resume handler failed', err);
    }
  }

  /** Convenience for the picker's Skip control. */
  async skip(interruptId: string): Promise<void> {
    await this.resolve(interruptId, { skipped: true });
  }

  /**
   * Register the chat layer's resume callback. Replaces any existing handler;
   * the chat layer is the single owner.
   */
  setResumeHandler(handler: UserQuestionResumeHandler | null): void {
    this.resumeHandler = handler;
  }
}
