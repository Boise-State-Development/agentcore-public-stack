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
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunService, withCreatedVersion } from './workflow-run.service';
import { AgentHandoffService, HandoffLaunch } from './agent-handoff.service';
import { HandoffStateService } from './handoff-state.service';
import { launchOf } from './workflow-run-queue.service';
import { RunEnvelope, WorkflowRunView } from './workflow-run.models';

/** Version pinning on a saved workflow's sheet and its steps' first turns (workflow-designer §23, PR-D.8c). */

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

/** A prompt step (workflow-designer §21.3): its worker is a model, judged as the starter. */
function promptSavedStep(name: string, reads: string[], model: Partial<NonNullable<WorkflowStepView['model']>> = {}): WorkflowStepView {
  const view = { canRun: true, modelId: 'us.anthropic.claude-haiku-5-5', name: 'Haiku 5.5', inputPricePerMillionTokens: 1, ...model };
  return {
    name,
    kind: 'prompt',
    agentId: '',
    modelId: 'us.anthropic.claude-haiku-5-5',
    reads,
    request: `Read it as ${name}`,
    effectiveRequest: `Read it as ${name}`,
    agent: { canRun: view.canRun, name: view.canRun ? view.name : undefined, hasIcon: false, changed: false, runsDraft: false, modelId: view.modelId },
    model: view,
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

function pinned(name: string, agentId: string, pin: NonNullable<WorkflowStepView['pin']>, agent: Partial<WorkflowStepView['agent']> = {}): WorkflowStepView {
  return { ...savedStep(name, agentId, { publishedVersion: pin.offerVersion ?? pin.version, ...agent }), agentVersion: pin.version, pin };
}

const TWO = [
  { name: 'generalist', agentId: 'a1' },
  { name: 'colleague', agentId: 'a2' },
];

function createdWithRun(runId: string, versions: (number | undefined)[]): CreateWorkflowRunResponse {
  const base = created(TWO);
  return {
    ...base,
    run: { ...base.run, runId, steps: base.run.steps.map((s, i) => ({ ...s, stepId: `${runId}-s${i + 1}`, handoffId: `${runId}-h${i + 1}` })) },
    steps: base.steps.map((s, i) => ({
      ...s,
      stepId: `${runId}-s${i + 1}`,
      handoffId: `${runId}-h${i + 1}`,
      ...(versions[i] !== undefined ? { agentVersion: versions[i] } : {}),
    })),
  };
}

function configure(agentVersionPinning: boolean): void {
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ConfigService, useValue: { appApiUrl: () => API } },
      { provide: FEATURES, useValue: { handoffBrief: false, handoffs: true, workflowRuns: true, workflows: true, agentVersionPinning } },
      { provide: FileUploadService, useValue: { listSessionFiles: async (): Promise<FileMetadata[]> => [] } },
      { provide: MessageMapService, useValue: { getMessagesForSession: () => signal([]) } },
      { provide: ChatRequestService, useValue: { buildHandoffBriefRequest: vi.fn(), submitHandoffRequest: vi.fn() } },
      { provide: ChatStateService, useValue: { isSessionLoading: () => false, viewedSessionId: () => 'src' } },
      { provide: ChatHttpService, useValue: { cancelChatRequest: vi.fn() } },
      { provide: SessionService, useValue: { mergedSessionsResource: () => ({ sessions: [] }) } },
      { provide: ComposerDraftService, useValue: { request: vi.fn() } },
      { provide: ConversationBindingService, useValue: { queryParamsFor: () => ({}) } },
      { provide: Router, useValue: { navigate: vi.fn() } },
      { provide: ToastService, useValue: { info: vi.fn(), success: vi.fn(), warning: vi.fn(), error: vi.fn(), dismiss: vi.fn() } },
      { provide: HandoffNotificationService, useValue: { onStateChange: vi.fn(), canOffer: () => false } },
      { provide: HandoffBriefService, useValue: { preempt: vi.fn().mockResolvedValue(undefined), stream: vi.fn() } },
    ],
  });
}

describe('WorkflowRunService with version pinning on', () => {
  let service: WorkflowRunService;
  let state: WorkflowRunStateService;
  let http: HttpTestingController;

  beforeEach(() => {
    configure(true);
    service = TestBed.inject(WorkflowRunService);
    state = TestBed.inject(WorkflowRunStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('carries each step’s pin onto the sheet, and leaves an unpinned step without one', async () => {
    const document = doc([pinned('generalist', 'a1', { version: 3, canRun: true, offerVersion: 5 }), savedStep('colleague', 'a2')]);
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(createdWithRun('run1', [3, undefined]));
    await opened;
    const steps = state.reviewFor('src')!.steps;
    expect(steps[0].workflowStep?.pin).toEqual({ version: 3, canRun: true, offerVersion: 5 });
    expect(steps[1].workflowStep?.pin).toBeUndefined();
  });

  it('Use version for someone who can’t edit replaces the run with this run’s choice, keeping the sheet’s edits', async () => {
    const document = doc([pinned('generalist', 'a1', { version: 3, canRun: true, offerVersion: 5 }), savedStep('colleague', 'a2')], { isOwned: false });
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(createdWithRun('run1', [3, undefined]));
    await opened;
    const second = state.reviewFor('src')!.steps[1];
    service.setStepRequest('src', second.stepId, 'Only the budget');
    service.setCard('src', 'my edited card');

    const moved = service.useVersion('src', 'generalist');
    http.expectOne(`${RUNS}/run1/dismiss`).flush(null);
    const create = http.expectOne(RUNS);
    expect(create.request.body.workflow).toEqual({ skillId: 'skill-1', name: 'review', removedSteps: [], useVersions: { generalist: 5 } });
    create.flush(createdWithRun('run2', [5, undefined]));
    await moved;
    http.expectNone(WORKFLOW);

    const review = state.reviewFor('src')!;
    expect(review.phase).toBe('ready');
    expect(review.created?.run.runId).toBe('run2');
    expect(review.useVersions).toEqual({ generalist: 5 });
    expect(review.steps[0].workflowStep).toMatchObject({ pin: { version: 5, canRun: true }, pinForThisRun: true });
    expect(review.steps[1].ownRequest).toBe('Only the budget');
    expect(review.card).toBe('my edited card');
  });

  it('Use version for the workflow’s owner writes the file, then replaces the run with no override', async () => {
    const document = doc([pinned('generalist', 'a1', { version: 3, canRun: true, offerVersion: 5 }), savedStep('colleague', 'a2')]);
    const opened = service.open('src', draftFor(document));
    http.expectOne(RUNS).flush(createdWithRun('run1', [3, undefined]));
    await opened;

    const moved = service.useVersion('src', 'generalist');
    const put = http.expectOne(WORKFLOW);
    expect(put.request.method).toBe('PUT');
    expect(put.request.body.steps.map((s: { agentVersion: number }) => s.agentVersion)).toEqual([5, 1]);
    put.flush({ ...doc([pinned('generalist', 'a1', { version: 5, canRun: true }), savedStep('colleague', 'a2')], { version: 4 }), written: true });
    await vi.waitFor(() => http.expectOne(`${RUNS}/run1/dismiss`).flush(null));
    const create = http.expectOne(RUNS);
    expect(create.request.body.workflow.useVersions).toBeUndefined();
    create.flush(createdWithRun('run2', [5, undefined]));
    await moved;

    const review = state.reviewFor('src')!;
    expect(review.steps[0].workflowStep?.pin).toEqual({ version: 5, canRun: true });
    expect(review.workflow?.document.workflow.version).toBe(4);
  });

  it('a version above the published one blocks the sheet until Use version clears it, for this run only', async () => {
    const document = doc(
      [pinned('generalist', 'a1', { version: 5, canRun: false, reason: 'above_published', offerVersion: 3 }), savedStep('colleague', 'a2')],
      { isOwned: false },
      [
        {
          code: 'agent_version_unavailable',
          step: 'generalist',
          message: 'Version 5 of this Agent is no longer published; version 3 is the current one. Use version 3, or remove the step',
        },
      ],
    );
    await service.open('src', draftFor(document));
    http.expectNone(RUNS);
    let review = state.reviewFor('src')!;
    expect(review.phase).toBe('blocked');
    expect(review.steps[0].workflowStep).toMatchObject({
      unavailable: true,
      unavailableReason: 'Version 5 of this Agent is no longer published; version 3 is the current one. Use version 3, or remove the step.',
    });

    const moved = service.useVersion('src', 'generalist');
    const create = http.expectOne(RUNS);
    expect(create.request.body.workflow.useVersions).toEqual({ generalist: 3 });
    create.flush(createdWithRun('run1', [3, undefined]));
    await moved;
    review = state.reviewFor('src')!;
    expect(review.phase).toBe('ready');
    expect(review.steps[0].workflowStep).toMatchObject({ unavailable: false, pin: { version: 3, canRun: true } });
  });

  it('a taken-down Agent’s step offers no version, and Remove still starts the run without it', async () => {
    const document = doc(
      [
        pinned('generalist', 'a1', { version: 3, canRun: false, reason: 'unpublished' }, { publishedVersion: undefined }),
        savedStep('colleague', 'a2'),
        savedStep('outsider', 'a3'),
      ],
      { isOwned: false },
      [{ code: 'agent_version_unavailable', step: 'generalist', message: "This Agent is no longer published, so version 3 can't run. Remove the step or choose another Agent" }],
    );
    await service.open('src', draftFor(document));
    const review = state.reviewFor('src')!;
    expect(review.phase).toBe('blocked');
    expect(review.steps[0].workflowStep?.pin?.offerVersion).toBeUndefined();
    await service.useVersion('src', 'generalist');
    http.expectNone(RUNS);

    service.removeStep('src', review.steps[0].stepId);
    const create = http.expectOne(RUNS);
    expect(create.request.body.workflow.removedSteps).toEqual(['generalist']);
    create.flush(createdWithRun('run1', [undefined, undefined]));
  });

  it('a pinned step’s first turn sends the version run create recorded; an unpinned one sends none', () => {
    const response = createdWithRun('run1', [3, undefined]);
    const run: WorkflowRunView = { ...response.run, workflow: { skillId: 'skill-1', name: 'review', title: 'Proposal review', fingerprint: 'fp' } };
    const launches = run.steps.map((step) => withCreatedVersion(launchOf(run, step), response));
    expect(launches.map((launch) => launch.agentVersion)).toEqual([3, undefined]);
    expect('agentVersion' in launches[1]).toBe(false);
  });
});

describe('WorkflowRunService with version pinning off', () => {
  let service: WorkflowRunService;
  let state: WorkflowRunStateService;
  let http: HttpTestingController;

  beforeEach(() => {
    configure(false);
    service = TestBed.inject(WorkflowRunService);
    state = TestBed.inject(WorkflowRunStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('shows no pin, offers no Use version and sends no override, as today', async () => {
    const document = doc([pinned('generalist', 'a1', { version: 3, canRun: true, offerVersion: 5 }, { changed: true }), savedStep('colleague', 'a2')], {
      isOwned: false,
    });
    const opened = service.open('src', draftFor(document));
    const create = http.expectOne(RUNS);
    expect(create.request.body.workflow).toEqual({ skillId: 'skill-1', name: 'review', removedSteps: [] });
    create.flush(createdWithRun('run1', [undefined, undefined]));
    await opened;
    expect(state.reviewFor('src')!.steps[0].workflowStep).toMatchObject({ changed: true });
    expect(state.reviewFor('src')!.steps[0].workflowStep?.pin).toBeUndefined();
    await service.useVersion('src', 'generalist');
    http.expectNone(RUNS);
    http.expectNone(WORKFLOW);
  });
});

describe('WorkflowRunQueueService, a pinned step’s first turn', () => {
  const ENVELOPE: RunEnvelope = {
    runId: 'run1',
    sourceSessionId: 'src',
    sourceTitle: 'Proposal',
    sourceMessageIndex: 3,
    excerptTokens: 20,
    fileIds: [],
    filesUnticked: 0,
    concurrency: 4,
  };

  function sendsWith(agentVersionPinning: boolean): HandoffLaunch[] {
    const sends: HandoffLaunch[] = [];
    TestBed.configureTestingModule({
      providers: [
        HandoffStateService,
        { provide: FEATURES, useValue: { agentVersionPinning } },
        {
          provide: AgentHandoffService,
          useValue: {
            startRunStep: (step: HandoffLaunch) => {
              sends.push(step);
              return new Promise<void>(() => undefined);
            },
            prepareRunStep: () => Promise.reject(new Error('no reads')),
          },
        },
      ],
    });
    TestBed.inject(WorkflowRunQueueService).enqueue(ENVELOPE, [
      { stepId: 's1', handoffId: 'h1', agentId: 'a1', agentName: 'A', request: 'go', agentVersion: 3 },
      { stepId: 's2', handoffId: 'h2', agentId: 'a2', agentName: 'B', request: 'go' },
    ]);
    return sends;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('sends agentVersion with the flag on, only for the pinned step', () => {
    const sends = sendsWith(true);
    expect(sends[0].agentVersion).toBe(3);
    expect('agentVersion' in sends[1]).toBe(false);
  });

  it('sends nothing new with the flag off', () => {
    expect(sendsWith(false).every((send) => !('agentVersion' in send))).toBe(true);
  });

  it('reads a reloaded run’s version from its row, never on a run of mentions or a model step', () => {
    const response = createdWithRun('run1', [3, undefined]);
    const steps = [{ ...response.run.steps[0], agentVersion: 3 }, { ...response.run.steps[1], agentVersion: 4, kind: 'prompt' as const }];
    const saved: WorkflowRunView = { ...response.run, steps, workflow: { skillId: 'skill-1', name: 'review', title: 'T', fingerprint: 'fp' } };
    expect(saved.steps.map((step) => launchOf(saved, step).agentVersion)).toEqual([3, undefined]);
    const mentions: WorkflowRunView = { ...saved, workflow: undefined };
    expect(launchOf(mentions, mentions.steps[0]).agentVersion).toBeUndefined();
  });
});
