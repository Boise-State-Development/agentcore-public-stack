import { describe, expect, it } from 'vitest';
import { routeMention } from './mention-routing';

/**
 * The SPA half of the `@`-mention binding rule, and the mirror of the backend's
 * `binds_conversation`. What these tests protect is a failure that was invisible from both
 * ends: the mentioned turn ran as the Agent, the next one did not, and the user was left in
 * a thread that still looked like the Agent's with its tools, skills and model gone.
 */
describe('routeMention', () => {
  const SESSION = 'sess-1';

  it('leaves an ordinary message alone', () => {
    expect(
      routeMention({
        boundAssistantId: undefined,
        sessionId: SESSION,
        threadHasMessages: true,
      }),
    ).toEqual({ sessionId: SESSION, assistantId: undefined, handedOff: false });
  });

  it('keeps a bound conversation bound when no one is mentioned', () => {
    expect(
      routeMention({
        boundAssistantId: 'ast-rubric',
        sessionId: SESSION,
        threadHasMessages: true,
      }),
    ).toEqual({ sessionId: SESSION, assistantId: 'ast-rubric', handedOff: false });
  });

  describe('mentioning into an empty thread', () => {
    it('binds the Agent to this conversation — mentioning is launching', () => {
      expect(
        routeMention({
          mentionedAgentId: 'ast-rubric',
          boundAssistantId: undefined,
          sessionId: SESSION,
          threadHasMessages: false,
        }),
      ).toEqual({ sessionId: SESSION, assistantId: 'ast-rubric', handedOff: false });
    });

    it('binds on a conversation that does not exist yet', () => {
      expect(
        routeMention({
          mentionedAgentId: 'ast-rubric',
          sessionId: null,
          threadHasMessages: false,
        }),
      ).toEqual({ sessionId: null, assistantId: 'ast-rubric', handedOff: false });
    });

    it('lets the mention win over an Agent the empty thread merely pointed at', () => {
      // Launched Agent A from its card, then typed `@B` before ever sending. Nothing has
      // happened yet, so there is no history for B's binding to misrepresent.
      expect(
        routeMention({
          mentionedAgentId: 'ast-b',
          boundAssistantId: 'ast-a',
          sessionId: SESSION,
          threadHasMessages: false,
        }),
      ).toEqual({ sessionId: SESSION, assistantId: 'ast-b', handedOff: false });
    });
  });

  describe('mentioning into a thread that already has history', () => {
    it('hands the message to a NEW conversation with that Agent', () => {
      // The case that produced "Unknown tool: create_rubric". The Agent cannot be bound to
      // history written under other instructions, and must not be borrowed for one turn,
      // so it gets a conversation of its own.
      expect(
        routeMention({
          mentionedAgentId: 'ast-rubric',
          boundAssistantId: undefined,
          sessionId: SESSION,
          threadHasMessages: true,
        }),
      ).toEqual({ sessionId: null, assistantId: 'ast-rubric', handedOff: true });
    });

    it('hands off out of a thread bound to a different Agent too', () => {
      expect(
        routeMention({
          mentionedAgentId: 'ast-b',
          boundAssistantId: 'ast-a',
          sessionId: SESSION,
          threadHasMessages: true,
        }),
      ).toEqual({ sessionId: null, assistantId: 'ast-b', handedOff: true });
    });

    it('does NOT hand off when the mention names the Agent already running the thread', () => {
      // Re-mentioning the bound Agent is emphasis, not a request for a new thread.
      expect(
        routeMention({
          mentionedAgentId: 'ast-rubric',
          boundAssistantId: 'ast-rubric',
          sessionId: SESSION,
          threadHasMessages: true,
        }),
      ).toEqual({ sessionId: SESSION, assistantId: 'ast-rubric', handedOff: false });
    });
  });

  it('never returns the mentioned Agent without also making it the bound assistant', () => {
    // The invariant the old behaviour broke: an Agent ran a turn that no later turn could
    // reproduce, because nothing carried it forward.
    for (const threadHasMessages of [true, false]) {
      const routed = routeMention({
        mentionedAgentId: 'ast-rubric',
        boundAssistantId: 'ast-other',
        sessionId: SESSION,
        threadHasMessages,
      });
      expect(routed.assistantId).toBe('ast-rubric');
    }
  });
});
