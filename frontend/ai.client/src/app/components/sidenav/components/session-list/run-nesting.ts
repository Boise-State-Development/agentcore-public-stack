import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { RunLink } from '../../../../session/services/handoff/workflow-run-sidebar.service';

/** A workflow run's step conversations, listed under their source (workflow-runs spec §20.4). */
export interface SessionRunGroup {
  runId: string;
  sourceSessionId: string;
  /** In the run's step order where it is known, else the list's order. */
  steps: SessionMetadata[];
}

export interface NestedBucket {
  /** The bucket's conversations with its nested steps taken out, in the list's order. */
  sessions: SessionMetadata[];
  /** The run groups each source hosts, by source session id, first-listed run first. */
  runsBySource: ReadonlyMap<string, SessionRunGroup[]>;
}

/**
 * Nest a time bucket's step conversations under their source.
 *
 * A step nests only when its source is in the **same bucket** on the loaded page and can host a
 * group (`canHost`: a project task's rows are grouped under the project instead). Otherwise it is
 * listed alone, as before. A conversation nested as a step hosts nothing itself, so a run started
 * from a step conversation lists its own steps alone rather than two levels deep.
 */
export function nestRunSteps(
  sessions: readonly SessionMetadata[],
  linkOf: (session: SessionMetadata) => RunLink | null,
  canHost: (source: SessionMetadata) => boolean,
): NestedBucket {
  const byId = new Map(sessions.map((session) => [session.sessionId, session] as const));
  const links = new Map<string, RunLink>();
  for (const session of sessions) {
    const link = linkOf(session);
    if (link && link.sourceSessionId !== session.sessionId) links.set(session.sessionId, link);
  }

  const nested = new Map<string, boolean>();
  const isNested = (sessionId: string, seen: Set<string>): boolean => {
    const known = nested.get(sessionId);
    if (known !== undefined) return known;
    const link = links.get(sessionId);
    const source = link ? byId.get(link.sourceSessionId) : undefined;
    let result = false;
    if (link && source && canHost(source) && !seen.has(source.sessionId)) {
      seen.add(sessionId);
      result = !isNested(source.sessionId, seen);
    }
    nested.set(sessionId, result);
    return result;
  };

  const kept: SessionMetadata[] = [];
  const runsBySource = new Map<string, SessionRunGroup[]>();
  for (const session of sessions) {
    if (!isNested(session.sessionId, new Set())) {
      kept.push(session);
      continue;
    }
    const link = links.get(session.sessionId)!;
    const groups = runsBySource.get(link.sourceSessionId) ?? [];
    let group = groups.find((g) => g.runId === link.runId);
    if (!group) {
      group = { runId: link.runId, sourceSessionId: link.sourceSessionId, steps: [] };
      groups.push(group);
      runsBySource.set(link.sourceSessionId, groups);
    }
    group.steps.push(session);
  }
  return { sessions: kept, runsBySource };
}
