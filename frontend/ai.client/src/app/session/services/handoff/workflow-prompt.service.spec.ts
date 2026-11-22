import { TestBed } from '@angular/core/testing';
import type { EventSourceMessage, FetchEventSourceInit } from '@microsoft/fetch-event-source';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';
import { SessionAffinityService } from '../session/session-affinity.service';
import { PromptStepFrame, WORKFLOW_PROMPT_SSE, WorkflowPromptService, workflowPromptBody } from './workflow-prompt.service';

describe('workflowPromptBody', () => {
  it('is the source session and the steps, and nothing a turn sends', () => {
    expect(workflowPromptBody('src', 'run1', ['s1', 's2', 's1'])).toEqual({
      session_id: 'src',
      workflow_prompt: { runId: 'run1', stepIds: ['s1', 's2'] },
    });
  });

  it('names at most eight steps', () => {
    const ids = Array.from({ length: 10 }, (_, i) => `s${i}`);
    expect((workflowPromptBody('src', 'run1', ids)['workflow_prompt'] as { stepIds: string[] }).stepIds).toHaveLength(8);
  });
});

describe('WorkflowPromptService', () => {
  let service: WorkflowPromptService;
  let init: FetchEventSourceInit | undefined;
  let url: string | undefined;
  let script: (init: FetchEventSourceInit) => Promise<void>;

  beforeEach(() => {
    init = undefined;
    script = async () => undefined;
    TestBed.configureTestingModule({
      providers: [
        { provide: ConfigService, useValue: { appApiUrl: () => 'https://api.test/' } },
        { provide: BffSessionService, useValue: { csrfHeaders: () => ({ 'X-CSRF-Token': 'tok' }) } },
        { provide: SessionAffinityService, useValue: { affinityFor: (id: string) => (id === 'fork' ? 'root' : null) } },
        {
          provide: WORKFLOW_PROMPT_SSE,
          useValue: (input: string, options: FetchEventSourceInit) => {
            url = input;
            init = options;
            return script(options);
          },
        },
      ],
    });
    service = TestBed.inject(WorkflowPromptService);
  });

  afterEach(() => TestBed.resetTestingModule());

  const message = (event: string, data: unknown): EventSourceMessage => ({ id: '', event, data: JSON.stringify(data) });

  it('posts to the chat proxy with the CSRF header and the fork pin', async () => {
    await service.run('fork', 'run1', ['s1'], { onStep: () => undefined, onRefused: () => undefined });
    expect(url).toBe('https://api.test/chat/stream');
    expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok');
    expect(JSON.parse(init?.body as string)).toEqual({
      session_id: 'fork',
      workflow_prompt: { runId: 'run1', stepIds: ['s1'] },
      affinity_session_id: 'root',
    });
  });

  it('hands each step’s frame over once, and ignores another run’s', async () => {
    const frames: PromptStepFrame[] = [];
    script = async (options) => {
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's1', status: 'done' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's1', status: 'done' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'other', stepId: 's9', status: 'done' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's2', status: 'stopped', stoppedReason: 'too_large' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's3', status: 'skipped', reason: 'reads_pending' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's4', status: 'skipped', reason: 'something new' }));
      options.onmessage?.(message('done', { runId: 'run1' }));
    };
    await service.run('src', 'run1', ['s1', 's2', 's3', 's4'], { onStep: (f) => frames.push(f), onRefused: () => undefined });
    expect(frames).toEqual([
      { stepId: 's1', status: 'done' },
      { stepId: 's2', status: 'stopped', stoppedReason: 'too_large' },
      { stepId: 's3', status: 'skipped', reason: 'reads_pending' },
      // An unknown skip is read as not_open, so the row is re-read rather than resent.
      { stepId: 's4', status: 'skipped', reason: 'not_open' },
    ]);
  });

  it('carries a frame’s kind, so a checklist step settles as one (PR-D.7c)', async () => {
    const frames: PromptStepFrame[] = [];
    script = async (options) => {
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's1', status: 'done', kind: 'checklist' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's2', status: 'stopped', stoppedReason: 'error', kind: 'checklist' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's3', status: 'skipped', reason: 'not_prompt', kind: 'checklist' }));
      // A kind this build doesn't know is left off rather than guessed.
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's4', status: 'done', kind: 'survey' }));
    };
    await service.run('src', 'run1', ['s1', 's2', 's3', 's4'], { onStep: (f) => frames.push(f), onRefused: () => undefined });
    expect(frames).toEqual([
      { stepId: 's1', status: 'done', kind: 'checklist' },
      { stepId: 's2', status: 'stopped', stoppedReason: 'error', kind: 'checklist' },
      { stepId: 's3', status: 'skipped', reason: 'not_prompt', kind: 'checklist' },
      { stepId: 's4', status: 'done' },
    ]);
  });

  it('reports a refused request and nothing after it', async () => {
    const onRefused = vi.fn();
    const onStep = vi.fn();
    script = async (options) => {
      options.onmessage?.(message('workflow_prompt_refused', { runId: 'run1', reason: 'not_approved' }));
      options.onmessage?.(message('workflow_prompt_step', { runId: 'run1', stepId: 's1', status: 'done' }));
    };
    await service.run('src', 'run1', ['s1'], { onStep, onRefused });
    expect(onRefused).toHaveBeenCalledWith('not_approved');
    expect(onStep).not.toHaveBeenCalled();
  });

  it('resolves, never rejects, when the stream fails', async () => {
    script = async () => {
      throw new Error('network');
    };
    await expect(service.run('src', 'run1', ['s1'], { onStep: () => undefined, onRefused: () => undefined })).resolves.toBeUndefined();
  });
});
