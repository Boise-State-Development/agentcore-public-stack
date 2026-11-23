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
import { ChatHttpService } from '../chat/chat-http.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { Message } from '../models/message.model';
import { ComposerDraftService } from '../session/composer-draft.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { HandoffBriefHandlers, HandoffBriefService } from './handoff-brief.service';
import { HandoffNotificationService } from './handoff-notification.service';
import { buildHandoffMessage, referenceUploadId, wrapHandoffContext } from './handoff-prompt';
import { HandoffStateService } from './handoff-state.service';
import { estimateRunInputUsd, formatEstimateUsd, tokenizerFactor } from './workflow-run-estimate';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { CreateWorkflowRunResponse, RunDraft, WorkflowRunView } from './workflow-run.models';
import { WorkflowRunService } from './workflow-run.service';

const API = 'https://api.test';
const RUNS = `${API}/sessions/src/workflow-runs`;
const CARD = '<conversation_excerpt turns="1" of="1">\nUser: review this proposal\n</conversation_excerpt>';
const AGENTS = ['Generalist', 'Colleague', 'Emotional outsider', 'Fiscal outsider', 'Auditor', 'Editor'];

function runView(overrides: Partial<WorkflowRunView> = {}): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'open',
    request: 'review this proposal',
    steps: AGENTS.map((name, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: `a${i + 1}`,
      agentName: name,
      request: 'review this proposal',
      removed: false,
      handoff: null,
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 3,
    createdAt: '2026-11-20T00:00:00Z',
    updatedAt: '2026-11-20T00:00:00Z',
    ...overrides,
  };
}

function created(count = AGENTS.length): CreateWorkflowRunResponse {
  const run = runView({ steps: runView().steps.slice(0, count) });
  const contextBlock = wrapHandoffContext(CARD, 'Proposal');
  return {
    run,
    excerpt: CARD,
    excerptHash: 'hash',
    excerptTokens: 20,
    turnsCovered: { first: 1, last: 2, of: 2, omitted: 0 },
    contextBlock,
    sourceMessageIndex: 3,
    sourceTitle: 'Proposal',
    steps: run.steps.map((step, i) => ({
      stepId: step.stepId,
      handoffId: step.handoffId,
      agentId: step.agentId,
      agentName: step.agentName,
      request: step.request,
      prompt: buildHandoffMessage(contextBlock, step.request),
      ...(i === 0 ? { modelId: 'us.anthropic.claude-sonnet-4-6', inputPricePerMillionTokens: 3 } : {}),
    })),
  };
}

function draft(count = AGENTS.length): RunDraft {
  return {
    agents: AGENTS.slice(0, count).map((name, i) => ({ agentId: `a${i + 1}`, agentName: name })),
    request: 'review this proposal',
  };
}

describe('WorkflowRunService', () => {
  let service: WorkflowRunService;
  let state: WorkflowRunStateService;
  let handoffs: HandoffStateService;
  let queue: WorkflowRunQueueService;
  let http: HttpTestingController;
  let features: { handoffBrief: boolean; handoffs: boolean; workflowRuns: boolean };
  let sends: { options: Record<string, unknown>; resolve: () => void }[];
  const draftRequest = vi.fn();
  const cancelChatRequest = vi.fn();
  const loading = new Set<string>();
  const toastInfo = vi.fn();
  const buildHandoffBriefRequest = vi.fn().mockResolvedValue({ message: 'x' });
  let briefHandlers: HandoffBriefHandlers | undefined;
  let sourceFiles: FileMetadata[] = [];
  let sourceMessages: Message[] = [];

  beforeEach(() => {
    draftRequest.mockClear();
    cancelChatRequest.mockClear();
    loading.clear();
    toastInfo.mockClear();
    buildHandoffBriefRequest.mockClear();
    briefHandlers = undefined;
    sends = [];
    sourceFiles = [];
    sourceMessages = [];
    features = { handoffBrief: false, handoffs: true, workflowRuns: true };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useFactory: () => features },
        { provide: FileUploadService, useValue: { listSessionFiles: async () => sourceFiles } },
        { provide: MessageMapService, useValue: { getMessagesForSession: () => signal(sourceMessages) } },
        {
          provide: ChatRequestService,
          useValue: {
            buildHandoffBriefRequest,
            submitHandoffRequest: (options: Record<string, unknown>) =>
              new Promise<void>((resolve) => sends.push({ options, resolve })),
          },
        },
        { provide: ChatStateService, useValue: { isSessionLoading: (id: string) => loading.has(id), viewedSessionId: () => 'src' } },
        { provide: ChatHttpService, useValue: { cancelChatRequest } },
        { provide: SessionService, useValue: { mergedSessionsResource: () => ({ sessions: [{ sessionId: 'src', title: 'Proposal' }] }) } },
        { provide: ComposerDraftService, useValue: { request: draftRequest } },
        { provide: ConversationBindingService, useValue: { queryParamsFor: (id: string) => ({ agent: id }) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
        { provide: ToastService, useValue: { info: toastInfo, success: vi.fn(), warning: vi.fn(), dismiss: vi.fn() } },
        { provide: HandoffNotificationService, useValue: { onStateChange: vi.fn(), canOffer: () => false } },
        {
          provide: HandoffBriefService,
          useValue: {
            preempt: vi.fn().mockResolvedValue(undefined),
            stream: (_s: string, _h: string, _b: unknown, handlers: HandoffBriefHandlers) => {
              briefHandlers = handlers;
              return Promise.resolve();
            },
          },
        },
      ],
    });
    service = TestBed.inject(WorkflowRunService);
    state = TestBed.inject(WorkflowRunStateService);
    handoffs = TestBed.inject(HandoffStateService);
    queue = TestBed.inject(WorkflowRunQueueService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  async function openReady(count = AGENTS.length): Promise<void> {
    const opened = service.open('src', draft(count));
    const req = http.expectOne(RUNS);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      request: 'review this proposal',
      steps: AGENTS.slice(0, count).map((_, i) => ({ agentId: `a${i + 1}` })),
    });
    req.flush(created(count));
    await opened;
    await Promise.resolve();
  }

  async function approve(body: Partial<WorkflowRunView> = {}): Promise<Record<string, unknown>> {
    const started = service.start('src');
    const req = http.expectOne(`${RUNS}/run1/approve`);
    const sentBody = req.request.body as Record<string, unknown>;
    const review = created().run;
    req.flush(runView({ ...review, status: 'approved', ...body }));
    await started;
    return sentBody;
  }

  it('opening asks for the run and its one card, and sends nothing to any Agent', async () => {
    await openReady();
    const review = state.reviewFor('src');
    expect(review?.phase).toBe('ready');
    expect(review?.card).toBe(CARD);
    expect(review?.steps.map((s) => s.agentName)).toEqual(AGENTS);
    expect(review?.steps[0].modelId).toBe('us.anthropic.claude-sonnet-4-6');
    expect(sends).toEqual([]);
  });

  it('cancel dismisses the run and gives the request back to the composer', async () => {
    await openReady();
    service.cancel('src');
    http.expectOne(`${RUNS}/run1/dismiss`).flush(null, { status: 204, statusText: 'No Content' });
    expect(state.reviewFor('src')).toBeUndefined();
    expect(draftRequest).toHaveBeenCalledWith('src', 'review this proposal');
  });

  it('a run cancelled while loading is dismissed once it arrives', async () => {
    const opened = service.open('src', draft());
    service.cancel('src');
    http.expectOne(RUNS).flush(created());
    await opened;
    http.expectOne(`${RUNS}/run1/dismiss`).flush(null, { status: 204, statusText: 'No Content' });
  });

  it('removing steps keeps at least one', async () => {
    await openReady(2);
    service.removeStep('src', 's1');
    service.removeStep('src', 's2');
    const kept = state.reviewFor('src')?.steps.filter((s) => !s.removed);
    expect(kept?.map((s) => s.stepId)).toEqual(['s2']);
  });

  it('previews each step with its own request and its own file lines', async () => {
    sourceFiles = [
      { uploadId: 'f1', filename: 'proposal.pdf', mimeType: 'application/pdf', sizeBytes: 10, sessionId: 'src', status: 'ready', createdAt: '' },
    ];
    await openReady(2);
    service.setFileShared('src', 'f1', true);
    service.setStepRequest('src', 's2', 'check the budget only');
    const review = state.reviewFor('src')!;
    const second = service.previewPrompt(review, 's2');
    expect(second).toContain('check the budget only');
    expect(second).toContain(referenceUploadId('f1', 'h2'));
    expect(service.previewPrompt(review, 's1')).toContain('review this proposal');
  });

  it('asks for one brief for the run, as its first step with the run id', async () => {
    features.handoffBrief = true;
    await openReady();
    await vi.waitFor(() => expect(buildHandoffBriefRequest).toHaveBeenCalled());
    expect(buildHandoffBriefRequest).toHaveBeenCalledWith(
      expect.objectContaining({ sourceSessionId: 'src', handoffId: 'h1', runId: 'run1' }),
    );
    expect(state.reviewFor('src')?.brief?.status).toBe('writing');
    await vi.waitFor(() => expect(briefHandlers).toBeDefined());
    briefHandlers!.onText('One brief');
    briefHandlers!.onDone({ briefHash: 'b' });
    expect(state.reviewFor('src')?.card).toBe('One brief');
    expect(state.reviewFor('src')?.brief?.status).toBe('written');
  });

  it('start approves the envelope as reviewed', async () => {
    sourceFiles = [
      { uploadId: 'f1', filename: 'proposal.pdf', mimeType: 'application/pdf', sizeBytes: 10, sessionId: 'src', status: 'ready', createdAt: '' },
      { uploadId: 'f2', filename: 'notes.md', mimeType: 'text/markdown', sizeBytes: 10, sessionId: 'src', status: 'ready', createdAt: '' },
    ];
    await openReady();
    service.setFileShared('src', 'f1', true);
    service.removeStep('src', 's6');
    service.setStepRequest('src', 's2', 'just the budget');
    const body = await approve();
    expect(body).toEqual({
      request: 'review this proposal',
      steps: [
        { stepId: 's1' },
        { stepId: 's2', request: 'just the budget' },
        { stepId: 's3' },
        { stepId: 's4' },
        { stepId: 's5' },
        { stepId: 's6', removed: true },
      ],
      fileIds: ['f1'],
      filesUnticked: 1,
      card: { text: CARD, removed: false },
    });
  });

  it('starts at most four at once, each with exactly the approved files and the reviewed card', async () => {
    await openReady();
    service.setCard('src', 'edited card');
    await approve({ fileIds: ['f1', 'f2'] });

    expect(sends).toHaveLength(4);
    for (const send of sends) {
      expect(send.options['sharedFileIds']).toEqual(['f1', 'f2']);
      expect(send.options['contextText']).toBe('edited card');
    }
    expect(sends.map((s) => s.options['handoffId'])).toEqual(['h1', 'h2', 'h3', 'h4']);
    expect(toastInfo).toHaveBeenCalledWith('Handed off to 6 Agents');
    expect(state.reviewFor('src')).toBeUndefined();
    expect(queue.hasActiveRun()).toBe(true);
    // The rows are there, tagged with the run, before any stream starts.
    expect(handoffs.rowsFor('src').map((r) => [r.handoffId, r.runId, r.status])).toEqual([
      ['h1', 'run1', 'running'],
      ['h2', 'run1', 'running'],
      ['h3', 'run1', 'running'],
      ['h4', 'run1', 'running'],
    ]);
  });

  it('starts the next when a running step reaches needs_input, done or stopped', async () => {
    await openReady();
    await approve();
    expect(sends).toHaveLength(4);

    const row = handoffs.rowsFor('src').find((r) => r.handoffId === 'h2')!;
    handoffs.upsertRow({ ...row, status: 'needs_input' });
    TestBed.tick();
    expect(sends.map((s) => s.options['handoffId'])).toContain('h5');

    sends[0].resolve(); // h1's stream ended
    await vi.waitFor(() => expect(sends).toHaveLength(6));
    expect(sends.map((s) => s.options['handoffId'])).toEqual(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']);
  });

  it('a removed card is sent as no card on every step', async () => {
    await openReady(2);
    service.setCardRemoved('src', true);
    await approve({ steps: runView().steps.slice(0, 2) });
    expect(sends.map((s) => s.options['contextText'])).toEqual([undefined, undefined]);
  });

  it('a failed approve keeps the sheet with the error and starts nothing', async () => {
    await openReady();
    const started = service.start('src');
    http.expectOne(`${RUNS}/run1/approve`).flush({ detail: 'x' }, { status: 409, statusText: 'Conflict' });
    await started;
    expect(state.reviewFor('src')?.phase).toBe('ready');
    expect(state.reviewFor('src')?.error).toContain('already started');
    expect(sends).toEqual([]);
  });

  it('sends the card as reviewed with the approval, or that it was removed', async () => {
    await openReady(2);
    service.setCard('src', 'reviewed card');
    expect((await approve({ steps: runView().steps.slice(0, 2) }))['card']).toEqual({ text: 'reviewed card', removed: false });

    await openReady(2);
    service.setCardRemoved('src', true);
    expect((await approve({ steps: runView().steps.slice(0, 2) }))['card']).toEqual({ removed: true });
  });

  it('resumes only open, unstarted steps, from the run row’s own card and files (any browser)', () => {
    const base = runView({ status: 'approved', fileIds: ['f1'], card: { text: 'reviewed card', removed: false } });
    const started = (i: number) => ({
      handoffId: `h${i}`,
      sessionId: 'src',
      kind: 'mention' as const,
      status: 'done' as const,
      agentId: `a${i}`,
      agentName: AGENTS[i - 1],
      request: 'r',
      sourceMessageIndex: 3,
      excerptTokens: 20,
      createdAt: '',
      updatedAt: '',
      targetSessionId: `t${i}`,
    });
    const open = (i: number) => ({ ...started(i), status: 'open' as const, targetSessionId: undefined });
    const run = runView({
      ...base,
      steps: base.steps.map((step, i) => ({ ...step, handoff: i < 4 ? started(i + 1) : open(i + 1) })),
    });
    expect(queue.unstartedSteps(run).map((s) => s.handoffId)).toEqual(['h5', 'h6']);
    expect(service.resume(run)).toBe(true);
    expect(sends.map((s) => [s.options['handoffId'], s.options['contextText'], s.options['sharedFileIds']])).toEqual([
      ['h5', 'reviewed card', ['f1']],
      ['h6', 'reviewed card', ['f1']],
    ]);
    expect(toastInfo).toHaveBeenCalledWith('Handed off to 2 Agents');
  });

  it('resumes a removed card as no card', () => {
    const run = runView({ status: 'approved', card: { removed: true } });
    run.steps = run.steps.slice(0, 1).map((step) => ({
      ...step,
      handoff: { handoffId: step.handoffId, sessionId: 'src', kind: 'mention', status: 'open', agentId: step.agentId, agentName: step.agentName, request: 'r', sourceMessageIndex: 3, excerptTokens: 20, createdAt: '', updatedAt: '' },
    }));
    expect(service.resume(run)).toBe(true);
    expect(sends.map((s) => s.options['contextText'])).toEqual([undefined]);
  });

  it('cannot resume a run approved before its card was kept on the row', () => {
    const run = runView({ status: 'approved' });
    expect(service.resume(run)).toBe(false);
    expect(sends).toEqual([]);
  });

  it('Stop run closes the run, starts no queued step and cancels each running step', async () => {
    await openReady();
    await approve();
    expect(sends).toHaveLength(4);
    for (const row of handoffs.rowsFor('src')) loading.add(row.targetSessionId!);

    const stopped = service.stop(state.runsFor('src')[0]);
    http.expectOne(`${RUNS}/run1/stop`).flush(null, { status: 204, statusText: 'No Content' });
    await stopped;

    expect(state.runsFor('src')[0].status).toBe('stopped');
    expect(cancelChatRequest).toHaveBeenCalledTimes(4);
    expect(queue.isQueued('run1')).toBe(false);
    sends[0].resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(sends).toHaveLength(4);
  });
});

describe('the run estimate', () => {
  it('applies the token profile factor per model', () => {
    expect(tokenizerFactor('us.anthropic.claude-haiku-4-5-20251001-v1:0')).toBe(0.95);
    expect(tokenizerFactor('us.anthropic.claude-sonnet-4-6')).toBe(0.95);
    expect(tokenizerFactor('us.anthropic.claude-sonnet-5-5')).toBe(1.4);
    expect(tokenizerFactor(null)).toBe(1.4);
  });

  it('prices every step: card, request and files, at its own rate and tokenizer', () => {
    const usd = estimateRunInputUsd({
      steps: [
        { modelId: 'claude-sonnet-4-6', inputPricePerMillionTokens: 3, request: '' },
        { modelId: 'claude-opus-5-5', inputPricePerMillionTokens: 5, request: '' },
      ],
      card: 'x'.repeat(4000), // 1,000 tokens
      fileTokens: [99_000],
    });
    // 100k tokens: 0.95 × 100k × $3/M + 1.40 × 100k × $5/M
    expect(usd).toBeCloseTo(0.285 + 0.7, 6);
    expect(formatEstimateUsd(usd!)).toBe('About $0.98');
  });

  it('is unavailable when a rate or a file count is missing', () => {
    expect(estimateRunInputUsd({ steps: [{ modelId: 'm', inputPricePerMillionTokens: null, request: '' }], card: '', fileTokens: [] })).toBeNull();
    expect(estimateRunInputUsd({ steps: [{ modelId: 'm', inputPricePerMillionTokens: 0, request: '' }], card: '', fileTokens: [] })).toBeNull();
    expect(estimateRunInputUsd({ steps: [{ modelId: 'm', inputPricePerMillionTokens: 3, request: '' }], card: '', fileTokens: [null] })).toBeNull();
  });
});
