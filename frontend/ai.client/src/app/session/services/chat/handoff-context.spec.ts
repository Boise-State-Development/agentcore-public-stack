import { describe, expect, it } from 'vitest';

import { handoffContextOf } from './chat-request.service';

/** `handoff_context` on a handoff send (handoffs spec §4.2, §4.9; workflow-designer §23.3). */
describe('handoffContextOf', () => {
  it('is the handoff alone when nothing else goes, as before pinning', () => {
    expect(JSON.stringify(handoffContextOf({ handoffId: 'h1' }))).toBe('{"handoffId":"h1"}');
  });

  it('keeps today’s field order for a card and shared files', () => {
    expect(JSON.stringify(handoffContextOf({ handoffId: 'h1', contextText: 'card', sharedFileIds: ['f1'], filesUnticked: 1 }))).toBe(
      '{"handoffId":"h1","text":"card","fileIds":["f1"],"filesUnticked":1}',
    );
  });

  it('adds a pinned step’s version last, and only when there is one', () => {
    expect(handoffContextOf({ handoffId: 'h1', contextText: '', agentVersion: 3 })).toEqual({ handoffId: 'h1', text: '', agentVersion: 3 });
    expect('agentVersion' in handoffContextOf({ handoffId: 'h1', agentVersion: undefined })).toBe(false);
  });
});
