import { describe, expect, it } from 'vitest';
import { isAwaitingUserSubmission } from './awaiting-submission';

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
