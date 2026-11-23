import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ChatStateService } from '../chat/chat-state.service';
import { HandoffView } from './handoff.models';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunSidebarService } from './workflow-run-sidebar.service';
import { WorkflowRunView } from './workflow-run.models';

/**
 * The sidebar's run line counts the steps it can list: those with a conversation. A prompt step
 * has none (workflow-designer §21.3, PR-D.6c), as `runSteps` and `isRunSettled` count a run.
 */

function handoff(handoffId: string, status: HandoffView['status'], prompt = false): HandoffView {
  return {
    handoffId,
    sessionId: 'src',
    kind: prompt ? 'prompt' : 'mention',
    status,
    agentId: prompt ? '' : `a-${handoffId}`,
    agentName: prompt ? 'Haiku 5.5' : `Agent ${handoffId}`,
    request: 'r',
    sourceMessageIndex: 0,
    excerptTokens: 0,
    createdAt: '',
    updatedAt: '',
    ...(prompt ? {} : { targetSessionId: `t-${handoffId}` }),
    runId: 'run1',
  };
}

function run(): WorkflowRunView {
  const rows = [handoff('h1', 'done'), handoff('h2', 'done'), handoff('h3', 'running', true)];
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'r',
    steps: rows.map((row, i) => ({
      stepId: `s${i + 1}`,
      handoffId: row.handoffId,
      ...(row.kind === 'prompt' ? { kind: 'prompt' as const } : {}),
      agentId: row.agentId,
      agentName: row.agentName,
      request: 'r',
      removed: false,
      handoff: row,
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 0,
    createdAt: '',
    updatedAt: '',
  };
}

describe('WorkflowRunSidebarService with prompt steps', () => {
  let sidebar: WorkflowRunSidebarService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => 'https://api.test' } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false } },
        { provide: WorkflowRunQueueService, useValue: { isPending: () => false, isRunning: () => false } },
      ],
    });
    sidebar = TestBed.inject(WorkflowRunSidebarService);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('counts only the steps with a conversation, and places them in the run’s order', () => {
    const progress = sidebar.progressFor(run());
    expect([progress.total, progress.done, progress.settled]).toEqual([2, 2, true]);
    expect(sidebar.stepIndex(run(), 't-h2')).toBe(1);
  });
});
