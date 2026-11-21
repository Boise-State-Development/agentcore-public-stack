import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { FileMetadata, FileUploadService } from '../../../services/file-upload/file-upload.service';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { WorkflowDocument, WorkflowStepView } from '../../../services/workflows/workflow.models';
import { ChatHttpService } from '../chat/chat-http.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { HandoffBriefService } from './handoff-brief.service';
import { HandoffNotificationService } from './handoff-notification.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { CreateWorkflowRunResponse, RunDraft } from './workflow-run.models';
import { WorkflowRunService, canStart } from './workflow-run.service';

/** A saved workflow's sheet in `WorkflowRunService` (workflow-designer.md §8.3, §10, §11, PR-D.2). */

const API = 'https://api.test';
const RUNS = `${API}/sessions/src/workflow-runs`;
const WORKFLOW = `${API}/workflows/skill-1/review`;

function savedStep(name: string, agentId: string, agent: Partial<WorkflowStepView['agent']> = {}, reads = ['request', 'files']): WorkflowStepView {
  return {
    name,
    kind: 'agent',
    agentId,
    agentVersion: 1,
    reads,
    effectiveRequest: 'Review this proposal',
    agent: { canRun: true, name: name.toUpperCase(), hasIcon: false, publishedVersion: 1, changed: false, runsDraft: false, ...agent },
  };
}

function doc(steps: WorkflowStepView[], overrides: Partial<WorkflowDocument['workflow']> = {}, blockers: WorkflowDocument['sheet']['blockers'] = []): WorkflowDocument {
  return {
    workflow: {
      skillId: 'skill-1',
      name: 'review',
      title: 'Proposal review',
      version: 3,
      files: 'any',
      request: 'Review this proposal',
      steps,
      result: { mode: 'card', steps: steps.map((s) => s.name) },
      fingerprint: 'fp',
      isOwned: true,
      ...overrides,
    },
    sheet: { startable: blockers.length === 0, blockers },
  };
}

function draftFor(document: WorkflowDocument): RunDraft {
  return {
    agents: document.workflow.steps.map((s) => ({ agentId: s.agentId, agentName: s.agent.name ?? '' })),
    request: document.workflow.request ?? '',
    workflow: { ref: { skillId: 'skill-1', name: 'review' }, document },
  };
}

function created(steps: { name: string; agentId: string }[]): CreateWorkflowRunResponse {
  return {
    run: {
      runId: 'run1',
      sessionId: 'src',
      status: 'open',
      request: 'Review this proposal',
      steps: steps.map((s, i) => ({
        stepId: `s${i + 1}`,
        handoffId: `h${i + 1}`,
        agentId: s.agentId,
        agentName: s.name.toUpperCase(),
        request: 'Review this proposal',
        removed: false,
        stepName: s.name,
      })),
      fileIds: [],
      limits: { maxSteps: 8, concurrency: 4 },
      sourceMessageIndex: -1,
      createdAt: '2026-11-20T00:00:00Z',
      updatedAt: '2026-11-20T00:00:00Z',
    },
    excerpt: '',
    excerptHash: 'h',
    excerptTokens: 0,
    contextBlock: '',
    sourceMessageIndex: -1,
    sourceTitle: 'Proposal review',
    steps: steps.map((s, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: s.agentId,
      agentName: s.name.toUpperCase(),
      request: 'Review this proposal',
      prompt: 'Review this proposal',
    })),
  };
}

describe('WorkflowRunService, for a saved workflow', () => {
  let service: WorkflowRunService;
  let state: WorkflowRunStateService;
  let http: HttpTestingController;
  let files: FileMetadata[];
  const toastError = vi.fn();

  beforeEach(() => {
    files = [];
    toastError.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { handoffBrief: true, handoffs: true, workflowRuns: true, workflows: true } },
        { provide: FileUploadService, useValue: { listSessionFiles: async () => files } },
        { provide: MessageMapService, useValue: { getMessagesForSession: () => signal([]) } },
        { provide: ChatRequestService, useValue: { buildHandoffBriefRequest: vi.fn(), submitHandoffRequest: vi.fn() } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false, viewedSessionId: () => 'src' } },
        { provide: ChatHttpService, useValue: { cancelChatRequest: vi.fn() } },
        { provide: SessionService, useValue: { mergedSessionsResource: () => ({ sessions: [] }) } },
        { provide: ComposerDraftService, useValue: { request: vi.fn() } },
        { provide: ConversationBindingService, useValue: { queryParamsFor: () => ({}) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
        { provide: ToastService, useValue: { info: vi.fn(), success: vi.fn(), warning: vi.fn(), error: toastError, dismiss: vi.fn() } },
        { provide: HandoffNotificationService, useValue: { onStateChange: vi.fn(), canOffer: () => false } },
        { provide: HandoffBriefService, useValue: { preempt: vi.fn().mockResolvedValue(undefined), stream: vi.fn() } },
      ],
    });
    service = TestBed.inject(WorkflowRunService);
    state = TestBed.inject(WorkflowRunStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('creates the run from the workflow, not from the Agents, and marks each step', async () => {
    const document = doc([savedStep('generalist', 'a1', { changed: true, publishedVersion: 6 }), savedStep('colleague', 'a2')]);
    const opened = service.open('src', draftFor(document));
    const req = http.expectOne(RUNS);
    expect(req.request.body).toEqual({
      request: 'Review this proposal',
      workflow: { skillId: 'skill-1', name: 'review', removedSteps: [] },
    });
    req.flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await opened;

    const review = state.reviewFor('src')!;
    expect(review.phase).toBe('ready');
    expect(review.steps.map((s) => s.workflowStep?.name)).toEqual(['generalist', 'colleague']);
    expect(review.steps[0].workflowStep).toMatchObject({ changed: true, publishedVersion: 6, unavailable: false });
    // An empty conversation has nothing to brief, so none is asked for.
    expect(review.brief).toBeUndefined();
  });

  it('a step the starter can’t run holds the sheet before the run exists, then creates it without that step and its readers', async () => {
    const document = doc(
      [
        savedStep('generalist', 'a1'),
        savedStep('colleague', 'a2'),
        savedStep('outsider', 'a3', { canRun: false, name: undefined }),
        savedStep('summary', 'a4', {}, ['outsider']),
      ],
      {},
      [{ code: 'step_unavailable', step: 'outsider', message: "You can't use this step's Agent" }],
    );
    await service.open('src', draftFor(document));
    http.expectNone(RUNS);
    let review = state.reviewFor('src')!;
    expect(review.phase).toBe('blocked');
    expect(canStart(review)).toBe(false);
    expect(review.steps.find((s) => s.workflowStep?.name === 'outsider')?.workflowStep?.unavailable).toBe(true);

    service.removeStep('src', 'workflow-step:outsider');
    review = state.reviewFor('src')!;
    expect(review.removedSteps).toEqual(['outsider', 'summary']);
    expect(review.phase).toBe('loading');
    const req = http.expectOne(RUNS);
    expect(req.request.body.workflow.removedSteps).toEqual(['outsider', 'summary']);
    req.flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await Promise.resolve();
    await Promise.resolve();
    expect(state.reviewFor('src')?.phase).toBe('ready');
  });

  it('on a ready sheet, removing a step removes every step that reads it, and approve sends them removed', async () => {
    const document = doc([
      savedStep('generalist', 'a1'),
      savedStep('colleague', 'a2'),
      savedStep('summary', 'a3', {}, ['request', 'generalist', 'colleague']),
      savedStep('editor', 'a4', {}, ['summary']),
    ]);
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(
      created([
        { name: 'generalist', agentId: 'a1' },
        { name: 'colleague', agentId: 'a2' },
        { name: 'summary', agentId: 'a3' },
        { name: 'editor', agentId: 'a4' },
      ]),
    );
    await opened;

    expect(service.removeStep('src', 's2')).toEqual(['s2', 's3', 's4']);
    expect(state.reviewFor('src')!.steps.map((s) => s.removed)).toEqual([false, true, true, true]);

    void service.start('src');
    const approve = http.expectOne(`${RUNS}/run1/approve`);
    expect(approve.request.body.steps).toEqual([
      { stepId: 's1' },
      { stepId: 's2', removed: true },
      { stepId: 's3', removed: true },
      { stepId: 's4', removed: true },
    ]);
    approve.flush({}, { status: 500, statusText: 'x' });
  });

  it('a step every other step reads stays, and a run of mentions removes one step at a time', async () => {
    const document = doc([savedStep('generalist', 'a1'), savedStep('summary', 'a2', {}, ['generalist'])]);
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'summary', agentId: 'a2' }]));
    await opened;

    expect(service.removeStep('src', 's1')).toEqual([]);
    expect(service.removeStep('src', 's2')).toEqual(['s2']);
  });

  it('anything else that blocks the workflow is the sheet’s error, and nothing is created', async () => {
    const document = doc([savedStep('generalist', 'a1')], {}, [{ code: 'too_few_steps', message: 'A workflow needs at least 2 steps to start' }]);
    await service.open('src', draftFor(document));
    http.expectNone(RUNS);
    expect(state.reviewFor('src')).toMatchObject({ phase: 'error', error: 'A workflow needs at least 2 steps to start.' });
  });

  it('shows the server’s blockers when create refuses', async () => {
    const document = doc([savedStep('generalist', 'a1'), savedStep('colleague', 'a2')]);
    const opened = service.open('src', draftFor(document));
    http
      .expectOne(RUNS)
      .flush(
        { detail: { code: 'workflow_blocked', message: "You can't use this step's Agent.", blockers: [] } },
        { status: 422, statusText: 'Unprocessable' },
      );
    await opened;
    expect(state.reviewFor('src')).toMatchObject({ phase: 'error', error: "You can't use this step's Agent." });
  });

  it('Mark as reviewed records the Agent’s published version in the file, for the owner', async () => {
    const document = doc([savedStep('generalist', 'a1', { changed: true, publishedVersion: 6 }), savedStep('colleague', 'a2')]);
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await opened;

    const marked = service.markReviewed('src', 'generalist');
    const put = http.expectOne(WORKFLOW);
    expect(put.request.method).toBe('PUT');
    expect(put.request.body.steps.map((s: { agentVersion: number }) => s.agentVersion)).toEqual([6, 1]);
    expect(put.request.body.version).toBeUndefined();
    const saved = doc([savedStep('generalist', 'a1', { publishedVersion: 6 }), savedStep('colleague', 'a2')], { version: 4 });
    put.flush({ ...saved, written: true });
    await marked;

    const review = state.reviewFor('src')!;
    expect(review.steps[0].workflowStep?.changed).toBe(false);
    expect(review.workflow?.document.workflow.version).toBe(4);
    expect(review.reviewingStep).toBeUndefined();
  });

  it('Mark as reviewed does nothing for someone the workflow is shared with', async () => {
    const document = doc([savedStep('generalist', 'a1', { changed: true, publishedVersion: 6 }), savedStep('colleague', 'a2')], { isOwned: false });
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await opened;
    await service.markReviewed('src', 'generalist');
    http.expectNone(WORKFLOW);
  });

  it('a workflow that takes one file ticks at most one, and swaps on a second tick', async () => {
    files = [
      { uploadId: 'f1', filename: 'a.pdf', mimeType: 'application/pdf', sizeBytes: 1, sessionId: 'src', status: 'ready', createdAt: '' },
      { uploadId: 'f2', filename: 'b.pdf', mimeType: 'application/pdf', sizeBytes: 1, sessionId: 'src', status: 'ready', createdAt: '' },
    ];
    const document = doc([savedStep('generalist', 'a1'), savedStep('colleague', 'a2')], { files: 'one' });
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await opened;
    await new Promise((resolve) => setTimeout(resolve));

    expect(canStart(state.reviewFor('src')!)).toBe(false);
    service.setFileShared('src', 'f1', true);
    service.setFileShared('src', 'f2', true);
    expect(state.reviewFor('src')?.fileIds).toEqual(['f2']);
    expect(canStart(state.reviewFor('src')!)).toBe(true);
  });
});
