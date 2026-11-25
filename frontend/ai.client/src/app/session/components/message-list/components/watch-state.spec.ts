import { describe, expect, it } from 'vitest';
import { withoutWatchState } from './watch-state';

describe('withoutWatchState', () => {
  it('drops the leading block a watcher run sends, keeping the schedule’s prompt', () => {
    const sent =
      '<watch_state>\n{"lastRunAt": null, "cursors": {}, "tracking": [], "seen": []}\n' +
      'This is an unattended watcher run. Call the report tool exactly once.\n</watch_state>\n\n' +
      'Tell me about new grants.';
    expect(withoutWatchState(sent)).toBe('Tell me about new grants.');
  });

  it('leaves a message without the block alone', () => {
    expect(withoutWatchState('What is <watch_state>?')).toBeNull();
    expect(withoutWatchState('Hello')).toBeNull();
  });
});
