import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { ToolInsightService } from './tool-insight.service';
import type {
  AgentStatusEvent,
  ToolGroupSummaryEvent,
} from '../../../shared/utils/stream-parser';

function status(overrides: Partial<AgentStatusEvent> = {}): AgentStatusEvent {
  return {
    type: 'agent_status',
    sessionId: 's1',
    phase: 'thinking',
    cycle: 1,
    ...overrides,
  };
}

function summary(
  overrides: Partial<ToolGroupSummaryEvent> = {},
): ToolGroupSummaryEvent {
  return {
    type: 'tool_group_summary',
    sessionId: 's1',
    batchId: 't1',
    toolUseIds: ['t1', 't2'],
    summary: 'Found the Syllabus Acknowledgment assignment in BIO 101',
    ...overrides,
  };
}

describe('ToolInsightService', () => {
  let service: ToolInsightService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = TestBed.inject(ToolInsightService);
  });

  describe('live status', () => {
    it('holds the latest transition for a conversation', () => {
      service.recordStatus('s1', status({ phase: 'tool_start', toolName: 'x' }));
      expect(service.status('s1')?.toolName).toBe('x');
    });

    it('keeps conversations apart', () => {
      // A background conversation's status must never narrate over the one on
      // screen.
      service.recordStatus('s1', status({ phase: 'tool_start', toolName: 'a' }));
      service.recordStatus('s2', status({ phase: 'tool_start', toolName: 'b' }));

      expect(service.status('s1')?.toolName).toBe('a');
      expect(service.status('s2')?.toolName).toBe('b');
    });

    it('clears on the turn falling edge', () => {
      service.recordStatus('s1', status());
      service.clearStatus('s1');
      expect(service.status('s1')).toBeUndefined();
    });

    it('clearing one conversation leaves the others running', () => {
      service.recordStatus('s1', status());
      service.recordStatus('s2', status());

      service.clearStatus('s1');

      expect(service.status('s1')).toBeUndefined();
      expect(service.status('s2')).toBeDefined();
    });

    it('is undefined for a conversation that never streamed', () => {
      expect(service.status('never-seen')).toBeUndefined();
    });
  });

  describe('durations', () => {
    it('records the measured duration from a tool_end', () => {
      service.recordStatus(
        's1',
        status({ phase: 'tool_end', toolUseId: 't1', durationMs: 250 }),
      );
      expect(service.get('s1', 't1')?.durationMs).toBe(250);
    });

    it('survives the turn ending', () => {
      // Durations are a durable fact about a call; only the "right now" claim
      // is ephemeral.
      service.recordStatus(
        's1',
        status({ phase: 'tool_end', toolUseId: 't1', durationMs: 250 }),
      );
      service.clearStatus('s1');

      expect(service.get('s1', 't1')?.durationMs).toBe(250);
    });

    it('ignores a tool_end with no duration', () => {
      service.recordStatus('s1', status({ phase: 'tool_end', toolUseId: 't1' }));
      expect(service.get('s1', 't1')?.durationMs).toBeUndefined();
    });

    it('does not invent a duration from a non-terminal phase', () => {
      service.recordStatus(
        's1',
        status({ phase: 'tool_start', toolUseId: 't1', durationMs: 999 }),
      );
      expect(service.get('s1', 't1')).toBeUndefined();
    });
  });

  describe('summaries', () => {
    it('attaches the batch summary to every call in the batch', () => {
      service.recordSummary('s1', summary());

      expect(service.get('s1', 't1')?.summary).toContain('Syllabus');
      expect(service.get('s1', 't2')?.summary).toContain('Syllabus');
    });

    it('finds a summary from any member of a group', () => {
      // The rail groups client-side and need not line up with the backend's
      // batches, so the lookup takes the whole group.
      service.recordSummary('s1', summary({ toolUseIds: ['t9'] }));

      expect(service.summaryFor('s1', ['t1', 't9'])).toContain('Syllabus');
    });

    it('returns undefined when nothing in the group was summarized', () => {
      expect(service.summaryFor('s1', ['t1', 't2'])).toBeUndefined();
    });

    it('does not leak a summary across conversations', () => {
      service.recordSummary('s1', summary());
      expect(service.summaryFor('s2', ['t1'])).toBeUndefined();
    });

    it('ignores an empty summary', () => {
      // Blanking a line that currently reads correctly is worse than leaving
      // the deterministic one in place.
      service.recordSummary('s1', summary({ summary: '   ' }));
      expect(service.summaryFor('s1', ['t1'])).toBeUndefined();
    });

    it('preserves a duration already recorded for the same call', () => {
      service.recordStatus(
        's1',
        status({ phase: 'tool_end', toolUseId: 't1', durationMs: 120 }),
      );
      service.recordSummary('s1', summary());

      const insight = service.get('s1', 't1');
      expect(insight?.durationMs).toBe(120);
      expect(insight?.summary).toContain('Syllabus');
    });

    it('is idempotent for a re-delivered event', () => {
      service.recordSummary('s1', summary());
      service.recordSummary('s1', summary());
      expect(service.summaryFor('s1', ['t1'])).toContain('Syllabus');
    });
  });

  describe('hydration', () => {
    it('seeds persisted summaries on conversation load', () => {
      service.seedFromHydration('s1', [
        { batchId: 't1', toolUseIds: ['t1', 't2'], summary: 'Listed 3 courses' },
      ]);

      expect(service.summaryFor('s1', ['t2'])).toBe('Listed 3 courses');
    });

    it('never clobbers a live summary that arrived first', () => {
      // A slow hydration response must not undo what the stream delivered
      // while it was in flight.
      service.recordSummary('s1', summary({ summary: 'Live line' }));
      service.seedFromHydration('s1', [
        { batchId: 't1', toolUseIds: ['t1'], summary: 'Stale persisted line' },
      ]);

      expect(service.summaryFor('s1', ['t1'])).toBe('Live line');
    });

    it('still seeds calls the live stream did not cover', () => {
      service.recordSummary('s1', summary({ toolUseIds: ['t1'], summary: 'Live' }));
      service.seedFromHydration('s1', [
        { batchId: 't5', toolUseIds: ['t5'], summary: 'Persisted' },
      ]);

      expect(service.summaryFor('s1', ['t1'])).toBe('Live');
      expect(service.summaryFor('s1', ['t5'])).toBe('Persisted');
    });

    it('skips rows with no summary or no ids', () => {
      service.seedFromHydration('s1', [
        { batchId: 'a', toolUseIds: ['t1'], summary: '' },
        { batchId: 'b', toolUseIds: [], summary: 'orphan' },
      ]);

      expect(service.summaryFor('s1', ['t1'])).toBeUndefined();
    });

    it('is a no-op for an empty sidecar', () => {
      service.seedFromHydration('s1', []);
      expect(service.summaryFor('s1', ['t1'])).toBeUndefined();
    });
  });

  describe('retention', () => {
    it('keeps insights after navigating away and back', () => {
      // Retention matches McpAppStateService: the events never re-stream, and
      // loadMessagesForSession skips the hydration fetch once a conversation
      // is cached — so a registry that reset on navigation would have no way
      // back to the prose line.
      service.recordSummary('s1', summary());
      service.recordStatus(
        's1',
        status({ phase: 'tool_end', toolUseId: 't1', durationMs: 90 }),
      );

      // Simulate visiting another conversation and returning.
      service.recordStatus('s2', status());
      service.clearStatus('s2');

      expect(service.summaryFor('s1', ['t1'])).toContain('Syllabus');
      expect(service.get('s1', 't1')?.durationMs).toBe(90);
    });
  });
});
