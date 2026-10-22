import { describe, expect, it } from 'vitest';
import { validateAgentStatusEvent } from './stream-parser-core';

/**
 * `agent_status` validation, and specifically the `preparing` phase
 * (docs/specs/agent-state-feedback.md PR-3).
 *
 * `preparing` is emitted by the chat route rather than the status hook,
 * because it describes the agent BUILD — which happens before the event loop,
 * and therefore before there is any cycle to number. The validator required
 * `cycle` to be a number when this phase was added, which would have dropped
 * every one of them silently: no error, no log, just a status line that never
 * said "Getting ready". That is exactly the failure this validator exists to
 * prevent for the other phases, so it is worth a test of its own.
 */
describe('validateAgentStatusEvent', () => {
  const base = { type: 'agent_status', sessionId: 's1' };

  describe('preparing', () => {
    it('accepts a preparing frame with no cycle', () => {
      expect(validateAgentStatusEvent({ ...base, phase: 'preparing' })).toBe(true);
    });

    it('still accepts one that happens to carry a cycle', () => {
      expect(
        validateAgentStatusEvent({ ...base, phase: 'preparing', cycle: 1 }),
      ).toBe(true);
    });
  });

  describe('prepared', () => {
    it('accepts a prepared frame with no cycle', () => {
      expect(validateAgentStatusEvent({ ...base, phase: 'prepared' })).toBe(true);
    });

    it('accepts the build duration it carries', () => {
      expect(
        validateAgentStatusEvent({ ...base, phase: 'prepared', durationMs: 1631 }),
      ).toBe(true);
    });
  });

  describe('the hook-sourced phases', () => {
    it('accepts them with a cycle', () => {
      for (const phase of ['thinking', 'tool_start', 'tool_end']) {
        expect(validateAgentStatusEvent({ ...base, phase, cycle: 2 })).toBe(true);
      }
    });

    it('still rejects them without one', () => {
      // The relaxation for `preparing` must not leak to the phases whose
      // cycle the SPA actually uses to tell one event-loop pass from another.
      for (const phase of ['thinking', 'tool_start', 'tool_end']) {
        expect(validateAgentStatusEvent({ ...base, phase })).toBe(false);
      }
    });
  });

  describe('rejections', () => {
    it('rejects an unknown phase', () => {
      expect(
        validateAgentStatusEvent({ ...base, phase: 'responding', cycle: 1 }),
      ).toBe(false);
    });

    it('rejects a frame of the wrong type', () => {
      expect(
        validateAgentStatusEvent({ ...base, type: 'tool_result', phase: 'preparing' }),
      ).toBe(false);
    });

    it('rejects non-objects', () => {
      expect(validateAgentStatusEvent(null)).toBe(false);
      expect(validateAgentStatusEvent('preparing')).toBe(false);
    });
  });
});
