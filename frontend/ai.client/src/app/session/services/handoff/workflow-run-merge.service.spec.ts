import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftStorageService } from '../session/composer-draft-storage.service';
import { ComposerEmptyService } from '../session/composer-empty.service';
import { DeliveryStateService } from './delivery-state.service';
import { DeliveryView, HandoffStatus, HandoffView } from './handoff.models';
import { HandoffNotificationService } from './handoff-notification.service';
import { HandoffStateService } from './handoff-state.service';
import { WorkflowRunMergeService, mergeInstructionOf } from './workflow-run-merge.service';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunResultService } from './workflow-run-result.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';

const API = 'https://api.test';
const COLLECT = `${API}/sessions/src/workflow-runs/run1/collect`;
const INSTRUCTION = 'Write one review that weighs every reviewer’s points.';

function row(i: number, status: HandoffStatus): HandoffView {
  return {
    handoffId: `h${i + 1}`,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `a${i + 1}`,
    agentName: `Agent ${i + 1}`,
    request: 'r',
    sourceMessageIndex: 1,
    excerptTokens: 1,
    createdAt: '',
    updatedAt: '',
    runId: 'run1',
    stepId: `s${i + 1}`,
    targetSessionId: `t${i + 1}`,
  };
}

function mergeRun(statuses: HandoffStatus[], workflow: Partial<NonNullable<WorkflowRunView['workflow']>> = {}): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'r',
    steps: statuses.map((status, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: `a${i + 1}`,
      agentName: `Agent ${i + 1}`,
      request: 'r',
      removed: false,
      handoff: row(i, status),
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 1,
    createdAt: '',
    updatedAt: '',
    workflow: {
      skillId: 'sk',
      name: 'review',
      title: 'Review',
      fingerprint: 'f',
      resultMode: 'merge',
      resultInstruction: INSTRUCTION,
      ...workflow,
    },
  };
}

function delivery(id = 'd1'): DeliveryView {
  return {
    deliveryId: id,
    fromSessionId: 't1',
    fromTitle: 'Review',
    origin: 'user',
    text: '',
    artifacts: [],
    createdAt: '2026-11-20T00:00:00Z',
    runId: 'run1',
    parts: [],
  };
}

describe('mergeInstructionOf', () => {
  it('is the instruction only for a merging Result', () => {
    expect(mergeInstructionOf(mergeRun(['done']))).toBe(INSTRUCTION);
    expect(mergeInstructionOf(mergeRun(['done'], { resultMode: 'card' }))).toBeNull();
    expect(mergeInstructionOf(mergeRun(['done'], { resultMode: undefined, resultInstruction: undefined }))).toBeNull();
    expect(mergeInstructionOf({ workflow: undefined })).toBeNull();
  });
});

describe('the merge that starts by itself', () => {
  let results: WorkflowRunResultService;
  let merge: WorkflowRunMergeService;
  let runs: WorkflowRunStateService;
  let handoffs: HandoffStateService;
  let deliveries: DeliveryStateService;
  let composer: ComposerEmptyService;
  let http: HttpTestingController;
  const loading = signal<ReadonlySet<string>>(new Set());
  const toast = { success: vi.fn(), info: vi.fn(), error: vi.fn() };
  let sent: string[];
  let sender: (text: string) => Promise<void>;

  beforeEach(() => {
    localStorage.clear();
    Object.values(toast).forEach((fn) => fn.mockClear());
    loading.set(new Set());
    sent = [];
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: ChatStateService, useValue: { isSessionLoading: (id: string) => loading().has(id) } },
        { provide: WorkflowRunQueueService, useValue: { isPending: () => false } },
        { provide: HandoffNotificationService, useValue: { openSource: vi.fn() } },
        { provide: ToastService, useValue: toast },
      ],
    });
    results = TestBed.inject(WorkflowRunResultService);
    merge = TestBed.inject(WorkflowRunMergeService);
    runs = TestBed.inject(WorkflowRunStateService);
    handoffs = TestBed.inject(HandoffStateService);
    deliveries = TestBed.inject(DeliveryStateService);
    composer = TestBed.inject(ComposerEmptyService);
    http = TestBed.inject(HttpTestingController);
    sender = async (text: string) => {
      // As the real send does, before its first await: the composer's cards ride it.
      deliveries.takeForSend('src');
      sent.push(text);
    };
  });

  afterEach(() => http.verify());

  /** A watched two-step run whose last step now finishes; returns once the collect has answered. */
  async function settle(answer: { created: boolean }, beforeAnswer?: () => void): Promise<void> {
    runs.upsertRun(mergeRun(['done', 'running']));
    results.watch({ runId: 'run1', sessionId: 'src' });
    TestBed.tick();
    handoffs.upsertRow(row(1, 'done'));
    TestBed.tick();
    const req = http.expectOne(COLLECT);
    beforeAnswer?.();
    req.flush({ deliveryId: 'd1', created: answer.created, status: 'pending', delivery: delivery() });
    await vi.waitFor(() => expect(deliveries.cardsFor('src').length + sent.length).toBeGreaterThan(0));
  }

  it('sends ONE turn, the instruction, with the run’s card, when the source is on screen, idle and empty', async () => {
    merge.register('src', sender);
    await settle({ created: true });

    expect(sent).toEqual([INSTRUCTION]);
    expect(deliveries.cardsFor('src')).toEqual([]);
    expect(toast.info).toHaveBeenCalledWith('Merging the results in this conversation');
    expect(toast.success).not.toHaveBeenCalledWith('Results added to your next message');
  });

  it('never merges from a collect that did not make the card (another tab or a reload won the claim)', async () => {
    merge.register('src', sender);
    await settle({ created: false });

    expect(sent).toEqual([]);
    expect(deliveries.cardsFor('src').map((c) => c.deliveryId)).toEqual(['d1']);
  });

  it('waits on the composer when the user has typed a draft', async () => {
    merge.register('src', sender);
    composer.note('src', { text: 'my own question' });
    await settle({ created: true });

    expect(sent).toEqual([]);
    expect(deliveries.cardsFor('src').map((c) => c.deliveryId)).toEqual(['d1']);
  });

  it('checks again at the moment of sending: typing after the run settled keeps the card', async () => {
    merge.register('src', sender);
    await settle({ created: true }, () => composer.note('src', { text: 'h' }));

    expect(sent).toEqual([]);
    expect(deliveries.cardsFor('src')).toHaveLength(1);
  });

  it('waits when the source is not the conversation on screen, or is streaming', async () => {
    await settle({ created: true });
    expect(sent).toEqual([]);

    const r = runs.runsFor('src')[0];
    merge.register('src', sender);
    loading.set(new Set(['src']));
    expect(merge.holdFor(r, 'd1')).toBe('busy');
    loading.set(new Set());
    expect(merge.holdFor(r, 'd1')).toBeNull();
  });

  it('the run’s own file left on the composer is no draft, but another file is', () => {
    const r = mergeRun(['done', 'done']);
    r.fileIds = ['up-1'];
    merge.register('src', sender);
    deliveries.add('src', delivery('d1'));
    const file = (uploadId: string) => ({ uploadId, filename: 'f.pdf', mimeType: 'application/pdf', sizeBytes: 1 });

    composer.note('src', { text: '', attachments: [file('up-1')] });
    expect(merge.holdFor(r, 'd1')).toBeNull();
    composer.note('src', { text: '', attachments: [file('up-1'), file('up-2')] });
    expect(merge.holdFor(r, 'd1')).toBe('composer_not_empty');
    composer.note('src', { text: 'and a question', attachments: [file('up-1')] });
    expect(merge.holdFor(r, 'd1')).toBe('composer_not_empty');
  });

  it('reads a draft left in storage before this tab’s composer reported one', async () => {
    TestBed.inject(ComposerDraftStorageService).write('src', { text: 'left over from before the reload' });
    merge.register('src', sender);
    await settle({ created: true });

    expect(sent).toEqual([]);
  });

  it('a card run never merges', async () => {
    merge.register('src', sender);
    runs.upsertRun(mergeRun(['done', 'running'], { resultMode: undefined, resultInstruction: undefined }));
    results.watch({ runId: 'run1', sessionId: 'src' });
    TestBed.tick();
    handoffs.upsertRow(row(1, 'done'));
    TestBed.tick();
    http.expectOne(COLLECT).flush({ deliveryId: 'd1', created: true, status: 'pending', delivery: delivery() });
    await vi.waitFor(() => expect(deliveries.cardsFor('src')).toHaveLength(1));
    expect(sent).toEqual([]);
  });

  it('another card waiting holds the automatic merge, but not Merge now; a draft holds neither once pressed', () => {
    const r = mergeRun(['done', 'done']);
    runs.upsertRun(r);
    merge.register('src', sender);
    deliveries.add('src', delivery('other'));
    deliveries.add('src', delivery('d1'));
    composer.note('src', { text: 'draft' });

    expect(merge.startIfReady(r, 'd1')).toBe(false);
    expect(merge.holdFor(r, 'd1')).toBe('other_cards');
    expect(merge.holdFor(r, 'd1', { manual: true })).toBeNull();
    expect(merge.startNow(r, 'd1')).toBe(true);
    expect(sent).toEqual([INSTRUCTION]);
  });

  it('nothing to merge once the card has left the composer, or with an open sheet', () => {
    const r = mergeRun(['done', 'done']);
    merge.register('src', sender);
    expect(merge.holdFor(r, 'd1')).toBe('card_not_waiting');
    deliveries.add('src', delivery('d1'));
    expect(merge.holdFor(r, 'd1')).toBeNull();
    runs.setReview('src', { phase: 'ready' } as never);
    expect(merge.holdFor(r, 'd1')).toBe('sheet_open');
  });

  it('unregister removes only its own sender', () => {
    const r = mergeRun(['done']);
    deliveries.add('src', delivery('d1'));
    const first = merge.register('src', sender);
    const second = merge.register('src', sender.bind(null));
    first();
    expect(merge.holdFor(r, 'd1')).toBeNull();
    second();
    expect(merge.holdFor(r, 'd1')).toBe('not_on_screen');
  });
});

describe('ComposerEmptyService', () => {
  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({});
  });

  it('is seeded from the stored draft, then follows what the composer reports', () => {
    TestBed.inject(ComposerDraftStorageService).write('a', { text: 'hello' });
    const composer = TestBed.inject(ComposerEmptyService);

    expect(composer.isEmpty('a')).toBe(false);
    expect(composer.isEmpty('b')).toBe(true);
    composer.note('a', { text: '  ' });
    expect(composer.isEmpty('a')).toBe(true);
    composer.note('b', { text: '', attachments: [{ uploadId: 'u', filename: 'f', mimeType: 'x', sizeBytes: 1 }] });
    expect(composer.isEmpty('b')).toBe(false);
    expect(composer.isEmpty(null)).toBe(true);
  });
});
