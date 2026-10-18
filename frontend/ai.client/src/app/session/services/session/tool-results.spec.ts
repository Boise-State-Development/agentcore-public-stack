import { describe, expect, it } from 'vitest';
import { matchToolResultsToToolUses } from './tool-results';
import type { Message } from '../models/message.model';

/**
 * A reloaded conversation must agree with what streamed: an MCP App form left
 * open for the user keeps its `awaiting_user_submission` marker, so the frame
 * shows "Waiting for you" after a refresh too, not "Failed".
 */
describe('matchToolResultsToToolUses', () => {
  function conversation(toolResult: Record<string, unknown>): Message[] {
    return [
      {
        id: 'a1',
        role: 'assistant',
        content: [{ type: 'toolUse', toolUse: { toolUseId: 't1', name: 'issue_write', input: {} } }],
      },
      {
        id: 'u1',
        role: 'user',
        content: [{ type: 'toolResult', toolResult: { toolUseId: 't1', ...toolResult } }],
      },
    ] as unknown as Message[];
  }

  const resultOf = (messages: Message[]) =>
    messages[0].content[0].toolUse?.result as Record<string, unknown> | undefined;

  it('keeps structuredContent on the merged result', () => {
    const merged = matchToolResultsToToolUses(
      conversation({
        status: 'error',
        isError: true,
        content: [{ text: 'An interactive form has been shown to the user' }],
        structuredContent: { status: 'awaiting_user_submission' },
      }),
    );

    expect(resultOf(merged)).toEqual({
      status: 'error',
      content: [{ text: 'An interactive form has been shown to the user' }],
      structuredContent: { status: 'awaiting_user_submission' },
    });
  });

  it('adds no structuredContent key when the result had none', () => {
    const merged = matchToolResultsToToolUses(
      conversation({ status: 'success', content: [{ text: 'ok' }] }),
    );

    expect(resultOf(merged)).toEqual({ status: 'success', content: [{ text: 'ok' }] });
  });
});
