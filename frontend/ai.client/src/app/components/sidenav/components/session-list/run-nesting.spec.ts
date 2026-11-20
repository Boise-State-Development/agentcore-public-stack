import { describe, expect, it } from 'vitest';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { RunLink } from '../../../../session/services/handoff/workflow-run-sidebar.service';
import { nestRunSteps } from './run-nesting';

function s(id: string, from?: string, runId = 'r1', projectId?: string): SessionMetadata {
  return {
    sessionId: id, userId: 'u', title: id, status: 'active', createdAt: '', lastMessageAt: '', messageCount: 1,
    ...(from ? { spawnedFrom: { sessionId: from, runId } } : {}),
    ...(projectId ? { preferences: { projectId } } : {}),
  } as SessionMetadata;
}

const linkOf = (x: SessionMetadata): RunLink | null =>
  x.spawnedFrom?.runId ? { sourceSessionId: x.spawnedFrom.sessionId, runId: x.spawnedFrom.runId } : null;
const anyHost = () => true;

describe('nestRunSteps', () => {
  it('groups a bucket\'s steps under their source, one group per run, in the list\'s order', () => {
    const out = nestRunSteps([s('a', 'src'), s('src'), s('b', 'src', 'r2'), s('c', 'src')], linkOf, anyHost);
    expect(out.sessions.map((x) => x.sessionId)).toEqual(['src']);
    expect(out.runsBySource.get('src')!.map((g) => [g.runId, g.steps.map((x) => x.sessionId)])).toEqual([
      ['r1', ['a', 'c']],
      ['r2', ['b']],
    ]);
  });

  it('leaves a step alone when its source is not in the bucket or cannot host', () => {
    const out = nestRunSteps([s('a', 'gone'), s('p', undefined, 'r1', 'prj'), s('b', 'p')], linkOf, (x) => !x.preferences?.projectId);
    expect(out.sessions.map((x) => x.sessionId)).toEqual(['a', 'p', 'b']);
    expect(out.runsBySource.size).toBe(0);
  });

  it('nests one level only: a step conversation hosts nothing', () => {
    const out = nestRunSteps([s('src'), s('a', 'src'), s('b', 'a', 'r2')], linkOf, anyHost);
    expect(out.sessions.map((x) => x.sessionId)).toEqual(['src', 'b']);
    expect(out.runsBySource.get('src')![0].steps.map((x) => x.sessionId)).toEqual(['a']);
    expect(out.runsBySource.has('a')).toBe(false);
  });

  it('survives a cycle', () => {
    const out = nestRunSteps([s('a', 'b'), s('b', 'a')], linkOf, anyHost);
    expect(out.sessions.length + [...out.runsBySource.values()].flat().flatMap((g) => g.steps).length).toBe(2);
  });
});
