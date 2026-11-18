import { describe, expect, it } from 'vitest';
import { findSubmittedCall, isAwaitingUserSubmission } from './awaiting-submission';

describe('isAwaitingUserSubmission', () => {
  it('recognizes the marker on an error result', () => {
    expect(
      isAwaitingUserSubmission({
        structuredContent: { status: 'awaiting_user_submission', reason: 'r' },
      }),
    ).toBe(true);
  });

  it('rejects anything else', () => {
    for (const result of [
      null,
      undefined,
      {},
      { structuredContent: null },
      { structuredContent: 'awaiting_user_submission' },
      { structuredContent: { status: 'created' } },
    ]) {
      expect(isAwaitingUserSubmission(result)).toBe(false);
    }
  });
});

describe('findSubmittedCall', () => {
  const awaiting = {
    structuredContent: { status: 'awaiting_user_submission' },
  };
  const card = (toolName: string, isError = false, id = toolName) => ({
    id,
    toolName,
    isError,
  });

  it('returns the latest successful call of the same tool', () => {
    const cards = [
      card('ui_get'),
      card('issue_write', false, 'first'),
      card('issue_write', false, 'second'),
      card('search_repositories'),
    ];
    expect(findSubmittedCall(awaiting, 'issue_write', cards)?.id).toBe('second');
  });

  it('matches a Gateway target prefix on the frame tool name', () => {
    expect(
      findSubmittedCall(awaiting, 'github___issue_write', [card('issue_write')]),
    ).not.toBeNull();
  });

  it('ignores failed calls and other tools', () => {
    const cards = [card('issue_write', true), card('ui_get'), card('write')];
    expect(findSubmittedCall(awaiting, 'issue_write', cards)).toBeNull();
  });

  it('only applies while the tool result is waiting on the user', () => {
    const cards = [card('issue_write')];
    expect(
      findSubmittedCall({ structuredContent: { status: 'created' } }, 'issue_write', cards),
    ).toBeNull();
    expect(findSubmittedCall(null, 'issue_write', cards)).toBeNull();
    expect(findSubmittedCall(awaiting, '', cards)).toBeNull();
  });
});
