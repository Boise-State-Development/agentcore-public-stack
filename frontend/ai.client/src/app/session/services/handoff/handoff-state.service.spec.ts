import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

function row(handoffId: string, overrides: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: 'mention',
    status: 'running',
    agentId: 'rubric',
    agentName: 'Rubric Builder',
    request: 'draft a rubric for this',
    sourceMessageIndex: 3,
    excerptTokens: 120,
    createdAt: '2026-10-22T00:00:00Z',
    updatedAt: '2026-10-22T00:00:00Z',
    targetSessionId: `tgt-${handoffId}`,
    ...overrides,
  };
}

describe('HandoffStateService', () => {
  let state: HandoffStateService;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    state = TestBed.inject(HandoffStateService);
  });

  it('keys rows by source session, so another thread never sees them', () => {
    state.upsertRow(row('h1'));
    expect(state.rowsFor('src').map((r) => r.handoffId)).toEqual(['h1']);
    expect(state.rowsFor('other')).toEqual([]);
    expect(state.rowsFor(null)).toEqual([]);
  });

  it('replaces a row in place on a refreshed status', () => {
    state.upsertRow(row('h1'));
    state.upsertRow(row('h1', { status: 'done', statusLine: 'Drafted it' }));
    expect(state.rowsFor('src')).toHaveLength(1);
    expect(state.rowsFor('src')[0].status).toBe('done');
  });

  it('hydration takes the server view but keeps a row sent here that the server does not list yet', () => {
    state.upsertRow(row('sent-just-now'));
    state.upsertRow(row('h1', { status: 'running' }));
    state.hydrate('src', [row('h1', { status: 'done' })], null);

    const rows = state.rowsFor('src');
    expect(rows.map((r) => [r.handoffId, r.status])).toEqual([
      ['h1', 'done'],
      ['sent-just-now', 'running'],
    ]);
  });

  it('records where a target came from', () => {
    state.hydrate('tgt', [], { sessionId: 'src', handoffId: 'h1', sourceTitle: 'Course planning', agentName: 'Rubric Builder' });
    expect(state.spawnedFrom('tgt')?.sourceTitle).toBe('Course planning');
    expect(state.spawnedFrom('src')).toBeUndefined();
  });

  it('holds at most one review per thread and clears it', () => {
    state.setReview('src', {
      draft: { agentId: 'rubric', agentName: 'Rubric Builder', request: 'x' },
      phase: 'loading',
      request: 'x',
      card: '',
      cardRemoved: false,
    });
    state.updateReview('src', { phase: 'ready', card: 'ctx' });
    expect(state.reviewFor('src')?.card).toBe('ctx');
    expect(state.reviewFor('other')).toBeUndefined();
    state.clearReview('src');
    expect(state.reviewFor('src')).toBeUndefined();
  });
});
