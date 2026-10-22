import { describe, expect, it } from 'vitest';
import type { Message } from '../models/message.model';
import {
  STEER_CLOSE_TAG,
  STEER_OPEN_TAG,
  buildSteeringMessage,
  normalizeSteeringMessages,
  unwrapSteeringText,
} from './steering';

const wrap = (text: string) => `${STEER_OPEN_TAG}\n${text}\n${STEER_CLOSE_TAG}`;

describe('unwrapSteeringText', () => {
  it('returns the user words inside a steering block', () => {
    expect(unwrapSteeringText(wrap('use the other file'))).toBe('use the other file');
  });

  it('returns null for ordinary text', () => {
    expect(unwrapSteeringText('use the other file')).toBeNull();
    expect(unwrapSteeringText('')).toBeNull();
    expect(unwrapSteeringText(undefined)).toBeNull();
  });

  it('does not unwrap a message that merely mentions the tag', () => {
    // A user asking about the wrapper itself must keep their own words.
    const typed = `what does ${STEER_OPEN_TAG} mean?`;
    expect(unwrapSteeringText(typed)).toBeNull();
  });
});

describe('buildSteeringMessage', () => {
  it('mints an id outside the msg-{session}-{index} namespace', () => {
    const message = buildSteeringMessage('sess-1', 'entry-9', 'hi');
    // The live index counts client-visible messages, and a steer is not one
    // server-side — it rides inside the tool-result message. Sharing that
    // namespace would risk colliding with an id the server computes on reload.
    expect(message.id).toBe('steer-sess-1-entry-9');
    expect(message.id.startsWith('msg-')).toBe(false);
  });

  it('is a user message flagged as steering', () => {
    const message = buildSteeringMessage('sess-1', 'e1', 'use the other file');
    expect(message.role).toBe('user');
    expect(message.steering).toBe(true);
    expect(message.content).toEqual([{ type: 'text', text: 'use the other file' }]);
  });
});

describe('normalizeSteeringMessages', () => {
  const userMessage = (): Message => ({
    id: 'msg-sess-1-0',
    role: 'user',
    content: [{ type: 'text', text: 'summarize the repo' }],
  });

  const restoredSteer = (text = 'use the other file'): Message => ({
    id: 'msg-sess-1-2',
    role: 'user',
    content: [
      { type: 'toolResult', toolResult: { toolUseId: 't1', content: [] } },
      { type: 'text', text: wrap(text) },
    ],
  });

  it('unwraps the injected text and flags the message', () => {
    const [, normalized] = normalizeSteeringMessages([userMessage(), restoredSteer()]);
    expect(normalized.steering).toBe(true);
    expect(normalized.content).toEqual([{ type: 'text', text: 'use the other file' }]);
  });

  it('drops the tool results, which are already folded into their toolUse', () => {
    const [, normalized] = normalizeSteeringMessages([userMessage(), restoredSteer()]);
    expect(normalized.content.some((b) => b.type === 'toolResult')).toBe(false);
  });

  it('leaves an ordinary history untouched, by identity', () => {
    const history = [userMessage()];
    // Every reload of every unsteered conversation takes this path; it must
    // cost a scan and nothing else.
    expect(normalizeSteeringMessages(history)).toBe(history);
  });

  it('leaves a plain tool-result message alone', () => {
    const toolOnly: Message = {
      id: 'msg-sess-1-2',
      role: 'user',
      content: [{ type: 'toolResult', toolResult: { toolUseId: 't1', content: [] } }],
    };
    const history = [userMessage(), toolOnly];
    expect(normalizeSteeringMessages(history)).toBe(history);
  });

  it('joins two injections that rode the same message', () => {
    const doubled: Message = {
      id: 'msg-sess-1-2',
      role: 'user',
      content: [
        { type: 'toolResult', toolResult: { toolUseId: 't1', content: [] } },
        { type: 'text', text: wrap('first') },
        { type: 'text', text: wrap('second') },
      ],
    };
    const [normalized] = normalizeSteeringMessages([doubled]);
    expect(normalized.content).toEqual([{ type: 'text', text: 'first\n\nsecond' }]);
  });

  it('never touches assistant messages', () => {
    const assistant: Message = {
      id: 'msg-sess-1-1',
      role: 'assistant',
      content: [{ type: 'text', text: wrap('not a user message') }],
    };
    const history = [assistant];
    expect(normalizeSteeringMessages(history)).toBe(history);
  });
});
