import { Injectable, computed, signal } from '@angular/core';

/**
 * Pending tool-approval prompt surfaced by the backend when an MCP tool
 * flagged `needs_approval` in the catalog is about to run. The agent's
 * tool call is paused (Strands interrupt); the chat UI renders an inline
 * approve/decline prompt and resumes the same turn with the user's
 * decision via {@link ToolApprovalResumeHandler}.
 *
 * Refresh recovery: the backend persists a `PendingInterrupt` breadcrumb
 * (`kind: "tool_approval"`) on the same `done` event, so reloading the
 * page mid-prompt rehydrates it via `MessageMapService.hydratePendingInterrupts`
 * — same flow as OAuth consent.
 */
export interface ToolApprovalRequest {
  interruptId: string;
  toolUseId: string;
  toolName: string;
  /** JSON-encoded tool input arguments. Pre-stringified by the backend. */
  toolInput?: string;
  message: string;
  receivedAt: number;
  /** Id of the assistant message whose tool call triggered this prompt;
   *  used by the inline message renderer to anchor the prompt. */
  messageId?: string;
  sessionId?: string;
}

export type ToolApprovalDecision = 'approved' | 'declined';

/**
 * Handler the chat layer registers to resume a paused agent turn after
 * the user makes an approval decision. Receives the interrupt id whose
 * tool call should now proceed (or be cancelled) plus the decision; the
 * handler is expected to POST to `/invocations` with the matching
 * `interrupt_responses` entry.
 */
export type ToolApprovalResumeHandler = (
  interruptId: string,
  decision: ToolApprovalDecision,
  context?: { sessionId?: string },
) => void | Promise<void>;

/**
 * Tracks per-tool approval prompts surfaced by the SSE stream and
 * coordinates their resolution. The stream parser calls
 * {@link requestApproval} when a `tool_approval_required` event arrives;
 * components render an approve/decline UI bound to {@link pending}.
 * When the user clicks Approve or Decline, {@link resolve} drops the
 * request locally and asks the registered {@link ToolApprovalResumeHandler}
 * to fire the resume request.
 */
@Injectable({ providedIn: 'root' })
export class ToolApprovalService {
  private readonly requests = signal<Map<string, ToolApprovalRequest>>(new Map());

  // Interrupt ids we've already surfaced this session, so a re-emission
  // (stream replay, network retry) doesn't resurrect a resolved prompt.
  // A new tool call always carries a fresh interrupt id, so legitimate
  // prompts aren't suppressed.
  private readonly seenInterruptIds = new Set<string>();

  private resumeHandler: ToolApprovalResumeHandler | null = null;

  readonly pending = computed<ToolApprovalRequest[]>(() =>
    Array.from(this.requests().values()).sort(
      (a, b) => a.receivedAt - b.receivedAt,
    ),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  /**
   * Register an approval request coming off the SSE stream. Idempotent
   * for the same interruptId — re-emission during a stream replay won't
   * resurrect a resolved prompt.
   */
  requestApproval(input: {
    interruptId: string;
    toolUseId: string;
    toolName: string;
    toolInput?: string;
    message: string;
    messageId?: string;
    sessionId?: string;
  }): void {
    if (this.seenInterruptIds.has(input.interruptId)) {
      return;
    }
    this.seenInterruptIds.add(input.interruptId);
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(input.interruptId, {
        ...input,
        receivedAt: Date.now(),
      });
      return next;
    });
  }

  /**
   * Resolve a pending approval. Drops the request locally and forwards
   * the decision to the registered resume handler so the chat layer can
   * POST a resume request. No-op if the request is unknown (already
   * resolved).
   */
  async resolve(interruptId: string, decision: ToolApprovalDecision): Promise<void> {
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
        'ToolApprovalService: no resume handler registered; decision dropped',
        { interruptId, decision },
      );
      return;
    }

    try {
      await this.resumeHandler(interruptId, decision, {
        sessionId: request.sessionId,
      });
    } catch (err) {
      console.error('ToolApprovalService: resume handler failed', err);
    }
  }

  /**
   * Register the chat layer's resume callback. Replaces any existing
   * handler; the chat layer is the single owner.
   */
  setResumeHandler(handler: ToolApprovalResumeHandler | null): void {
    this.resumeHandler = handler;
  }
}
