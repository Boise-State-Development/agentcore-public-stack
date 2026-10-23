import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { AgentHandoffService } from './agent-handoff.service';
import { buildHandoffMessage, wrapHandoffContext } from './handoff-prompt';
import { CreateHandoffResponse } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

const API = 'https://api.test';
const CARD = '<conversation_excerpt turns="1" of="1">\nUser: plan BIO 101\n</conversation_excerpt>';

function created(overrides: Partial<CreateHandoffResponse> = {}): CreateHandoffResponse {
  const contextBlock = wrapHandoffContext(CARD, 'Course planning');
  return {
    handoffId: 'h1',
    excerpt: CARD,
    excerptHash: 'hash',
    excerptTokens: 20,
    turnsCovered: { first: 1, last: 1, of: 1, omitted: 0 },
    contextBlock,
    prompt: buildHandoffMessage(contextBlock, 'draft a rubric'),
    request: 'draft a rubric',
    sourceMessageIndex: 5,
    sourceTitle: 'Course planning',
    ...overrides,
  };
}

describe('AgentHandoffService', () => {
  let service: AgentHandoffService;
  let state: HandoffStateService;
  let http: HttpTestingController;
  let loading: ReturnType<typeof signal<ReadonlySet<string>>>;
  const submitHandoffRequest = vi.fn().mockResolvedValue(undefined);
  const draftRequest = vi.fn();
  const toastInfo = vi.fn();
  const navigate = vi.fn().mockResolvedValue(true);

  beforeEach(() => {
    submitHandoffRequest.mockClear();
    draftRequest.mockClear();
    toastInfo.mockClear();
    loading = signal<ReadonlySet<string>>(new Set());
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: ChatRequestService, useValue: { submitHandoffRequest } },
        {
          provide: ChatStateService,
          useValue: { isSessionLoading: (id: string) => loading().has(id), viewedSessionId: () => 'src' },
        },
        { provide: ComposerDraftService, useValue: { request: draftRequest } },
        { provide: ConversationBindingService, useValue: { queryParamsFor: (id: string) => ({ agent: id }) } },
        { provide: Router, useValue: { navigate } },
        {
          provide: ToastService,
          useValue: { info: toastInfo, success: vi.fn(), warning: vi.fn() },
        },
      ],
    });
    service = TestBed.inject(AgentHandoffService);
    state = TestBed.inject(HandoffStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function openReady(): Promise<void> {
    const opened = service.open('src', { agentId: 'rubric', agentName: 'Rubric Builder', request: 'draft a rubric' });
    const req = http.expectOne(`${API}/sessions/src/handoffs`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ kind: 'mention', agentId: 'rubric', request: 'draft a rubric' });
    req.flush(created());
    await opened;
  }

  it('opening sends nothing to the Agent: it only asks for the card', async () => {
    await openReady();

    const review = state.reviewFor('src');
    expect(review?.phase).toBe('ready');
    expect(review?.card).toBe(CARD);
    expect(submitHandoffRequest).not.toHaveBeenCalled();
  });

  it('a failed card leaves an error in the sheet rather than sending', async () => {
    const opened = service.open('src', { agentId: 'gone', agentName: 'Gone', request: 'x' });
    http.expectOne(`${API}/sessions/src/handoffs`).flush({ detail: 'Agent not found' }, { status: 404, statusText: 'Not Found' });
    await opened;

    expect(state.reviewFor('src')?.phase).toBe('error');
    expect(state.reviewFor('src')?.error).toContain('isn’t available');
  });

  it('the preview is the server prompt until something is edited', async () => {
    await openReady();
    const review = () => state.reviewFor('src')!;
    expect(service.previewPrompt(review())).toBe(created().prompt);

    service.setRequest('src', 'draft a rubric, four rows');
    expect(service.previewPrompt(review())).toBe(`${created().contextBlock}\n\ndraft a rubric, four rows`);

    service.setCard('src', CARD.replace('BIO 101', 'BIO 102'));
    expect(service.previewPrompt(review())).toBe(
      buildHandoffMessage(wrapHandoffContext(CARD.replace('BIO 101', 'BIO 102'), 'Course planning'), 'draft a rubric, four rows'),
    );

    service.setCardRemoved('src', true);
    expect(service.previewPrompt(review())).toBe('draft a rubric, four rows');
  });

  it('cancel dismisses the handoff and gives the request back to the composer', async () => {
    await openReady();
    service.setRequest('src', 'draft a rubric, edited');

    service.cancel('src');

    http.expectOne(`${API}/sessions/src/handoffs/h1/dismiss`).flush(null);
    expect(state.reviewFor('src')).toBeUndefined();
    expect(draftRequest).toHaveBeenCalledWith('src', 'draft a rubric, edited');
    expect(submitHandoffRequest).not.toHaveBeenCalled();
  });

  it('send starts the target in the background, adds the row and stays put', async () => {
    await openReady();

    await service.send('src');

    expect(submitHandoffRequest).toHaveBeenCalledTimes(1);
    const sent = submitHandoffRequest.mock.calls[0][0];
    expect(sent).toMatchObject({ agentId: 'rubric', message: 'draft a rubric', handoffId: 'h1', contextText: CARD });
    expect(sent.targetSessionId).toMatch(/^[0-9a-f-]{36}$/);
    expect(navigate).not.toHaveBeenCalled();
    expect(state.reviewFor('src')).toBeUndefined();

    const [row] = state.rowsFor('src');
    expect(row).toMatchObject({ handoffId: 'h1', status: 'running', sourceMessageIndex: 5, targetSessionId: sent.targetSessionId });
    expect(state.spawnedFrom(sent.targetSessionId)?.sourceTitle).toBe('Course planning');
    expect(toastInfo).toHaveBeenCalledWith('Handed off to Rubric Builder', undefined, expect.anything());
  });

  it('a removed card is sent without context text', async () => {
    await openReady();
    service.setCardRemoved('src', true);

    await service.send('src');

    expect(submitHandoffRequest.mock.calls[0][0].contextText).toBeUndefined();
  });

  it('an empty request cannot be sent', async () => {
    await openReady();
    service.setRequest('src', '   ');

    await service.send('src');

    expect(submitHandoffRequest).not.toHaveBeenCalled();
  });

  it('a running target makes closing the tab ask first', async () => {
    await openReady();
    await service.send('src');
    const target = submitHandoffRequest.mock.calls[0][0].targetSessionId as string;

    expect(service.hasRunningHandoff()).toBe(false);
    loading.set(new Set([target]));
    expect(service.hasRunningHandoff()).toBe(true);
  });
});
