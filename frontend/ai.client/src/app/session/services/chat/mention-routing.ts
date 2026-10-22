/**
 * Where an `@`-mentioned Agent's message goes (the SPA half of the binding rule).
 *
 * This lives outside `session.page.ts` for the same reason `agent_binding_policy` lives
 * outside `chat/routes.py`: the rule is a handful of lines, the component around it has
 * thirty injected services, and a rule nobody can reach from a test is a rule that drifts.
 * It is the client-side mirror of `binds_conversation`, and the two must agree — the
 * backend decides whether a turn *binds*, this decides which conversation the turn is even
 * sent to.
 *
 * **A mention means "talk to this Agent", and it always yields a conversation where the
 * Agent keeps its tools.** It used to mean "borrow this Agent for one turn": the mentioned
 * turn ran as the Agent, and the next one silently did not. Nothing surfaced that — the
 * thread still looked like the Agent's, and the model, unaware its toolset had changed,
 * explained a vanished tool as something the user should fix in the tool picker. Measured
 * in prod, the borrow bought nothing to offset it: of 247 mentions, 247 *started* the
 * conversation and none was a mid-thread consult.
 *
 * So there are two outcomes and no third:
 *
 * - **Empty thread** — mentioning is launching. The Agent becomes the conversation's bound
 *   assistant. Safe precisely because there is no history for the binding to misrepresent.
 * - **Thread with history** — the Agent cannot be bound to history produced under other
 *   instructions, tools and skills, and it must not be borrowed either. The message opens a
 *   **new** conversation with the Agent, and the caller tells the user so.
 */

/** What the composer should actually submit, after a mention is taken into account. */
export interface MentionRouting {
  /** `null` asks `submitChatRequest` to create a conversation. */
  readonly sessionId: string | null;
  /** The conversation's bound Agent — goes on the URL and into `rag_assistant_id`. */
  readonly assistantId: string | undefined;
  /** True when the message was moved to a new conversation, so the caller can say so. */
  readonly handedOff: boolean;
}

export interface MentionRoutingInput {
  /** The Agent picked from the `@` menu, if any. */
  readonly mentionedAgentId?: string;
  /** The Agent this conversation is already bound to, if any. */
  readonly boundAssistantId?: string;
  /** The conversation being typed into; `null` when none exists yet. */
  readonly sessionId: string | null;
  /** Whether that conversation already has messages. */
  readonly threadHasMessages: boolean;
}

export function routeMention({
  mentionedAgentId,
  boundAssistantId,
  sessionId,
  threadHasMessages,
}: MentionRoutingInput): MentionRouting {
  // No mention, or a mention of the Agent already running this conversation. The second
  // case matters: re-mentioning the bound Agent is a no-op, and opening a new thread for
  // it would be an obnoxious answer to "yes, you, again".
  if (!mentionedAgentId || mentionedAgentId === boundAssistantId) {
    return { sessionId, assistantId: boundAssistantId, handedOff: false };
  }

  if (threadHasMessages) {
    return { sessionId: null, assistantId: mentionedAgentId, handedOff: true };
  }

  // Empty thread — including one already pointed at a *different* Agent the user never
  // sent to. The last name typed wins, because nothing has happened yet to contradict it.
  return { sessionId, assistantId: mentionedAgentId, handedOff: false };
}
