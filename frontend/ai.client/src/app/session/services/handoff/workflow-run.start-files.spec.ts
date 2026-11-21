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
import { ComposerDraftStorageService, StoredAttachment } from '../session/composer-draft-storage.service';
import { ComposerEmptyService } from '../session/composer-empty.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { HandoffBriefService } from './handoff-brief.service';
import { HandoffNotificationService } from './handoff-notification.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunResultService } from './workflow-run-result.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { CreateWorkflowRunResponse, RunDraft } from './workflow-run.models';
import { WorkflowRunService, canStart } from './workflow-run.service';

/**
 * Start takes a saved workflow's run files off the source composer (workflow-designer.md §20.3,
 * after #2256's dev check): only the run's own files, only once the approve succeeds.
 */

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

function attachment(uploadId: string): StoredAttachment {
  return { uploadId, filename: `${uploadId}.pdf`, mimeType: 'application/pdf', sizeBytes: 1 };
}

describe('WorkflowRunService Start, and the composer’s files', () => {
  let service: WorkflowRunService;
  let state: WorkflowRunStateService;
  let drafts: ComposerDraftStorageService;
  let composer: ComposerEmptyService;
  let http: HttpTestingController;
  const enqueue = vi.fn();

  beforeEach(() => {
    localStorage.clear();
    enqueue.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { handoffBrief: false, handoffs: true, workflowRuns: true, workflows: true } },
        {
          provide: FileUploadService,
          useValue: {
            listSessionFiles: async (): Promise<FileMetadata[]> => [
              { uploadId: 'f1', filename: 'f1.pdf', mimeType: 'application/pdf', sizeBytes: 1, sessionId: 'src', status: 'ready', createdAt: '' },
              { uploadId: 'f2', filename: 'f2.pdf', mimeType: 'application/pdf', sizeBytes: 1, sessionId: 'src', status: 'ready', createdAt: '' },
            ],
          },
        },
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
        { provide: WorkflowRunQueueService, useValue: { enqueue, isPending: () => false, stop: vi.fn(), queuedSources: () => [] } },
        { provide: WorkflowRunResultService, useValue: { watch: vi.fn() } },
      ],
    });
    service = TestBed.inject(WorkflowRunService);
    state = TestBed.inject(WorkflowRunStateService);
    drafts = TestBed.inject(ComposerDraftStorageService);
    composer = TestBed.inject(ComposerEmptyService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  /** A `files: one` sheet in `src`, f1 ticked, with the composer holding more than the run's file. */
  async function openWithDraft(mentions = false): Promise<void> {
    drafts.write('src', {
      text: 'my own question',
      queued: ['a follow-up'],
      attachments: [attachment('f1'), attachment('f2')],
      folders: [{ folderAttachId: 'fa1', name: 'Grants', source: 'drive' }],
    });
    composer.note('src', drafts.read('src'));
    const document = doc([savedStep('generalist', 'a1'), savedStep('colleague', 'a2')], { files: 'one' });
    const draft = draftFor(document);
    const opened = service.open('src', mentions ? { ...draft, workflow: undefined } : draft);
    http.expectOne(RUNS).flush(created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]));
    await opened;
    await new Promise((resolve) => setTimeout(resolve));
    service.setFileShared('src', 'f1', true);
  }

  function approved(fileIds: string[]) {
    const run = created([{ name: 'generalist', agentId: 'a1' }, { name: 'colleague', agentId: 'a2' }]).run;
    return { ...run, status: 'approved', fileIds, approvedAt: '2026-11-21T00:00:00Z', card: { removed: true } };
  }

  it('takes only the run’s own file off the composer once Start succeeds, and keeps the rest', async () => {
    await openWithDraft();
    const started = service.start('src');
    const approve = http.expectOne(`${RUNS}/run1/approve`);
    expect(approve.request.body.fileIds).toEqual(['f1']);
    approve.flush(approved(['f1']));
    await started;

    const after = drafts.read('src');
    expect(after.attachments?.map((a) => a.uploadId)).toEqual(['f2']);
    expect(after.text).toBe('my own question');
    expect(after.queued).toEqual(['a follow-up']);
    expect(after.folders?.map((f) => f.folderAttachId)).toEqual(['fa1']);
    expect(drafts.released()).toEqual({ key: 'src', uploadIds: ['f1'] });
    // The composer-empty signal follows: f2 and the text still make it a draft.
    expect(composer.isEmpty('src')).toBe(false);
    expect(enqueue).toHaveBeenCalledTimes(1);
  });

  it('with nothing else on the composer, it reads as empty after Start, and stays so from storage', async () => {
    await openWithDraft();
    drafts.write('src', { text: '', attachments: [attachment('f1')] });
    composer.note('src', drafts.read('src'));
    const started = service.start('src');
    http.expectOne(`${RUNS}/run1/approve`).flush(approved(['f1']));
    await started;

    expect(composer.isEmpty('src')).toBe(true);
    // A reload reads the stored row: a draft left with nothing is forgotten outright.
    expect(localStorage.getItem('composer-draft:src')).toBeNull();
  });

  it('a refused Start clears nothing', async () => {
    await openWithDraft();
    const started = service.start('src');
    http.expectOne(`${RUNS}/run1/approve`).flush({ detail: 'no' }, { status: 422, statusText: 'Unprocessable' });
    await started;

    expect(drafts.read('src').attachments?.map((a) => a.uploadId)).toEqual(['f1', 'f2']);
    expect(drafts.released()).toBeNull();
    expect(state.reviewFor('src')?.phase).toBe('ready');
  });

  it('Cancel clears nothing', async () => {
    await openWithDraft();
    service.cancel('src');
    http.match(() => true).forEach((req) => req.flush(null, { status: 204, statusText: 'No Content' }));

    expect(drafts.read('src').attachments?.map((a) => a.uploadId)).toEqual(['f1', 'f2']);
    expect(drafts.released()).toBeNull();
  });
});
