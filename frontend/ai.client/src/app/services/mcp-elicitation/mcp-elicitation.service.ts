import { Injectable, computed, signal } from '@angular/core';
import type { McpElicitationField } from '../../shared/utils/stream-parser/stream-parser-types';

/**
 * A form an external MCP server paused its tool call to ask the user to fill
 * in (`docs/specs/mcp-elicitation.md`).
 *
 * Sibling of `UserQuestionRequest`, and the flow is the same: the tool call is
 * paused (a Strands interrupt raised by the elicitation middleware), the chat
 * UI renders the form inline, and the answer resumes the same turn through
 * {@link McpElicitationResumeHandler}. On resume the backend replays the call
 * from its first round with the answer, so a slow answer never meets an
 * expired server state.
 */
export interface McpElicitationRequest {
  interruptId: string;
  toolUseId: string;
  toolName: string;
  /** The catalog row's display name: who is asking, as the admin named it. */
  serverName: string;
  message: string;
  fields: McpElicitationField[];
  receivedAt: number;
  /** Id of the assistant message whose tool call is paused. */
  messageId?: string;
  sessionId?: string;
}

/** One field's answer, typed by its kind. */
export type McpElicitationValue = string | number | boolean | string[];

/**
 * The payload posted back to resume the turn (spec §4).
 *
 * Always an object, never null: Strands treats a null response as no answer
 * and the resume route refuses one before the lease. `decline` is the form's
 * Decline button; `cancel` is the dismissal. The backend checks `content`
 * against the server's schema again and sends `cancel` for anything that does
 * not fit, so this side's validation is for the user, not for safety.
 */
export type McpElicitationResponse =
  | { action: 'accept'; content: Record<string, McpElicitationValue> }
  | { action: 'decline' }
  | { action: 'cancel' };

export type McpElicitationResumeHandler = (
  interruptId: string,
  response: McpElicitationResponse,
  context?: { sessionId?: string },
) => void | Promise<void>;

/**
 * Tracks MCP elicitation forms surfaced by the SSE stream (and replayed from
 * the `mcp_elicitation` breadcrumb on reload) and coordinates their answers.
 * The stream parser calls {@link requestInput}; the message list renders a
 * form per entry in {@link pending}; the form calls {@link resolve}.
 */
@Injectable({ providedIn: 'root' })
export class McpElicitationService {
  private readonly requests = signal<Map<string, McpElicitationRequest>>(new Map());

  // A re-emission of an answered form (stream replay, a reload racing the
  // resume) must not bring it back. A server that asks again gets a fresh
  // interrupt id, because the backend folds the round into the name.
  private readonly seenInterruptIds = new Set<string>();

  private resumeHandler: McpElicitationResumeHandler | null = null;

  readonly pending = computed<McpElicitationRequest[]>(() =>
    Array.from(this.requests().values()).sort((a, b) => a.receivedAt - b.receivedAt),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  /** Register a form. Idempotent for the same interrupt id. */
  requestInput(input: Omit<McpElicitationRequest, 'receivedAt'>): void {
    if (this.seenInterruptIds.has(input.interruptId)) {
      return;
    }
    if (!input.fields.length) {
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
   * Answer a pending form. Drops it locally and forwards the response to the
   * resume handler. A second call for the same id (a double click) is a no-op.
   */
  async resolve(interruptId: string, response: McpElicitationResponse): Promise<void> {
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
      console.warn('McpElicitationService: no resume handler registered; answer dropped', {
        interruptId,
      });
      return;
    }

    try {
      await this.resumeHandler(interruptId, response, { sessionId: request.sessionId });
    } catch (err) {
      console.error('McpElicitationService: resume handler failed', err);
    }
  }

  /** The form's Decline button. */
  async decline(interruptId: string): Promise<void> {
    await this.resolve(interruptId, { action: 'decline' });
  }

  /**
   * Drop a form without answering it: its pause is gone (a fresh turn
   * superseded it). It stays seen, so a late re-emission can't bring it back.
   */
  withdraw(interruptId: string): void {
    this.requests.update((map) => {
      if (!map.has(interruptId)) return map;
      const next = new Map(map);
      next.delete(interruptId);
      return next;
    });
  }

  /** The chat layer's resume callback; it is the single owner. */
  setResumeHandler(handler: McpElicitationResumeHandler | null): void {
    this.resumeHandler = handler;
  }
}
