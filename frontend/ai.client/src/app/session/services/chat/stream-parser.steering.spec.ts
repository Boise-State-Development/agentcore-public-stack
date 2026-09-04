import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { SteeringService } from './steering.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';

/**
 * Mid-turn steering in the live thread (docs/specs/mid-turn-steering.md, PR-5).
 *
 * The thing worth testing here is ORDER. A steer lands at a tool boundary, and
 * on a `tool_use` stop reason the tool-calling assistant message is
 * deliberately kept active so its results can attach — so at ack time it is
 * still the *current* message. Appending the user's words straight to the
 * completed list would sort them ahead of the assistant turn they interrupted,
 * which reads as the user having spoken first.
 */
describe('StreamParserService — mid-turn steering', () => {
  let service: StreamParserService;
  let steering: SteeringService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
    steering = TestBed.inject(SteeringService);
    steering.reset();
    service.reset('s1');
  });

  afterEach(() => TestBed.resetTestingModule());

  const send = (event: string, data: unknown) =>
    service.parseEventSourceMessage('s1', event, data);

  const applied = (entryId: string, text: string) =>
    send('steering_applied', {
      type: 'steering_applied',
      sessionId: 's1',
      entryId,
      text,
    });

  /** An assistant message that ends by calling a tool (stopReason=tool_use). */
  function assistantCallsTool(): void {
    send('message_start', { role: 'assistant' });
    send('content_block_delta', { contentBlockIndex: 0, text: 'Looking…' });
    // tool_use keeps the message ACTIVE so its results can attach — which is
    // exactly the state a steer lands in.
    send('message_stop', { stopReason: 'tool_use' });
  }

  const texts = () =>
    service
      .allMessagesFor('s1')()
      .map((m) => ({
        role: m.role,
        steering: m.steering === true,
        text: m.content.find((b) => b.type === 'text')?.text ?? '',
      }));

  it('renders the follow-up after the assistant turn it interrupted', () => {
    assistantCallsTool();
    applied('e1', 'use the other file');

    expect(texts()).toEqual([
      { role: 'assistant', steering: false, text: 'Looking…' },
      { role: 'user', steering: true, text: 'use the other file' },
    ]);
  });

  it('keeps that order when the next assistant message starts', () => {
    assistantCallsTool();
    applied('e1', 'use the other file');

    send('message_start', { role: 'assistant' });
    send('content_block_delta', { contentBlockIndex: 0, text: 'On it.' });

    // The finalize that folds the first message into the completed list must
    // carry the steer with it, or the user's words jump to the end.
    expect(texts().map((m) => m.text)).toEqual([
      'Looking…',
      'use the other file',
      'On it.',
    ]);
  });

  it('flags the message so turn grouping does not split the response', () => {
    assistantCallsTool();
    applied('e1', 'hi');
    const steer = service.allMessagesFor('s1')().find((m) => m.role === 'user');
    expect(steer?.steering).toBe(true);
  });

  it('survives the turn ending', () => {
    assistantCallsTool();
    applied('e1', 'use the other file');
    send('done', null);

    expect(texts().map((m) => m.text)).toEqual(['Looking…', 'use the other file']);
  });

  it('renders two steers on one turn in arrival order', () => {
    assistantCallsTool();
    applied('e1', 'first');
    applied('e2', 'second');

    expect(texts().map((m) => m.text)).toEqual(['Looking…', 'first', 'second']);
  });

  it('ignores a replayed ack rather than rendering it twice', () => {
    assistantCallsTool();
    applied('e1', 'use the other file');
    applied('e1', 'use the other file');

    expect(texts().filter((m) => m.role === 'user')).toHaveLength(1);
  });

  it('notifies the composer so its queued copy is dropped', () => {
    assistantCallsTool();
    applied('e1', 'use the other file');

    // Not viewed-session-scoped: a background conversation's ack must still
    // clear the queued entry, or the user gets a duplicate on returning to it.
    expect(steering.applied()).toContain('e1');
  });

  it('lands the steer even with no message in flight', () => {
    // A boundary after the last assistant message finalized: there is nothing
    // current to sit behind, so it goes straight into the completed list.
    applied('e1', 'use the other file');
    send('done', null);

    expect(texts()).toEqual([
      { role: 'user', steering: true, text: 'use the other file' },
    ]);
  });

  it('marks the turn as tool-using so the composer can promise mid-turn delivery', () => {
    expect(steering.canSteer('s1')).toBe(false);
    send('tool_use', { tool_use: { name: 'read_file', tool_use_id: 't1', input: '{}' } });
    expect(steering.canSteer('s1')).toBe(true);
  });

  it('clears that flag when a new turn starts', () => {
    send('tool_use', { tool_use: { name: 'read_file', tool_use_id: 't1', input: '{}' } });
    service.reset('s1');
    // A fresh turn may be pure text, and over-promising is the failure mode
    // that teaches users not to trust the affordance.
    expect(steering.canSteer('s1')).toBe(false);
  });
});
