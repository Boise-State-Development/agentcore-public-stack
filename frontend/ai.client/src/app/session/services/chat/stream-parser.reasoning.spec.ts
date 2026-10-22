import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';
import { ContentBlock } from '../models/message.model';

/**
 * Reasoning durations — the "Thought for 17s" header
 * (docs/specs/agent-state-feedback.md PR-1).
 *
 * The span is client-observed, so what is worth pinning is WHEN it closes: too
 * early and it undercounts, too late and it silently absorbs whatever the model
 * did next. The cases below are the three closing points plus the two ways the
 * duration is deliberately absent.
 */
describe('StreamParserService — reasoning duration', () => {
  let service: StreamParserService;
  let clock: number;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
    service.reset('s1');

    // A hand-cranked clock beats fake timers here: the parser schedules a 5s
    // flush on `done`, and we want to move time without tripping it.
    clock = 1_000_000;
    vi.spyOn(Date, 'now').mockImplementation(() => clock);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  const send = (event: string, data: unknown) =>
    service.parseEventSourceMessage('s1', event, data);

  const tick = (ms: number) => {
    clock += ms;
  };

  /** The reasoning block of the newest assistant message, if there is one. */
  const reasoningBlock = (): ContentBlock | undefined =>
    service
      .allMessagesFor('s1')()
      .flatMap((m) => m.content)
      .find((block) => block.type === 'reasoningContent');

  it('closes the span on the first text delta and reports whole seconds', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'Let me check the roster' });
    tick(17_000);
    send('content_block_delta', { contentBlockIndex: 1, text: 'Here is' });
    tick(4_000); // answer time must NOT land in the reasoning number

    expect(reasoningBlock()?.reasoningDurationMs).toBe(17_000);
  });

  it('closes the span on a tool block, not on the tool result that follows', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'I should look this up' });
    tick(3_000);
    send('content_block_start', {
      contentBlockIndex: 1,
      type: 'tool_use',
      toolUse: { toolUseId: 't1', name: 'list_assignments' },
    });
    tick(9_000); // the tool runs; none of this is thinking

    expect(reasoningBlock()?.reasoningDurationMs).toBe(3_000);
  });

  it('falls back to message_stop for a cycle that only reasoned', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'Hmm' });
    tick(2_000);
    send('message_stop', { stopReason: 'end_turn' });

    expect(reasoningBlock()?.reasoningDurationMs).toBe(2_000);
  });

  it('reports no duration while the model is still thinking', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'Still going' });
    tick(5_000);
    send('reasoning', { reasoningText: ' and going' });

    expect(reasoningBlock()).toBeDefined();
    expect(reasoningBlock()?.reasoningDurationMs).toBeUndefined();
  });

  it('keeps the span open across consecutive reasoning deltas', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'one' });
    tick(1_000);
    send('reasoning', { reasoningText: 'two' });
    tick(1_000);
    send('reasoning', { reasoningText: 'three' });
    tick(1_000);
    send('content_block_delta', { contentBlockIndex: 1, text: 'Done' });

    // The whole run of deltas, not just the last gap.
    expect(reasoningBlock()?.reasoningDurationMs).toBe(3_000);
  });

  it('does not re-open or extend a closed span', () => {
    send('message_start', { role: 'assistant' });
    send('reasoning', { reasoningText: 'thinking' });
    tick(2_000);
    send('content_block_delta', { contentBlockIndex: 1, text: 'answer' });
    tick(6_000);
    send('content_block_delta', { contentBlockIndex: 1, text: ' continues' });
    send('message_stop', { stopReason: 'end_turn' });

    expect(reasoningBlock()?.reasoningDurationMs).toBe(2_000);
  });

  it('leaves the duration off a message with no reasoning at all', () => {
    send('message_start', { role: 'assistant' });
    send('content_block_delta', { contentBlockIndex: 0, text: 'Straight to it' });
    send('message_stop', { stopReason: 'end_turn' });

    expect(reasoningBlock()).toBeUndefined();
  });
});
