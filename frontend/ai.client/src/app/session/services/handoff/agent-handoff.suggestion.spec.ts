import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { FileUploadService } from '../../../services/file-upload/file-upload.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { MessageMapService } from '../session/message-map.service';
import { AgentHandoffService } from './agent-handoff.service';
import { buildHandoffMessage, wrapHandoffContext } from './handoff-prompt';
import { CreateHandoffResponse, HandoffView, isSuggestionChip } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

const API = 'https://api.test';
const CARD = '<conversation_excerpt turns="1" of="1">\nUser: plan BIO 101\n</conversation_excerpt>';
const PROMPT = 'Draft a four-level rubric for the BIO 101 lab report.';

const CHIP: HandoffView = {
  handoffId: 's1',
  sessionId: 'src',
  kind: 'suggested',
  status: 'open',
  agentId: '',
  agentName: '',
  request: '',
  sourceMessageIndex: -1,
  excerptTokens: 0,
  createdAt: '2026-11-19T00:00:00Z',
  updatedAt: '2026-11-19T00:00:00Z',
  title: 'Draft the BIO 101 rubric',
  tldr: 'The rubric is its own deliverable.',
  agentHint: 'Rubric Builder',
  toolUseId: 'tu-1',
};

function started(): CreateHandoffResponse {
  const contextBlock = wrapHandoffContext(CARD, 'Course planning');
  return {
    handoffId: 's1',
    excerpt: CARD,
    excerptHash: 'hash',
    excerptTokens: 20,
    contextBlock,
    prompt: buildHandoffMessage(contextBlock, PROMPT),
    request: PROMPT,
    sourceMessageIndex: 1,
    sourceTitle: 'Course planning',
  };
}

describe('AgentHandoffService — suggestion chips (§4.4)', () => {
  let service: AgentHandoffService;
  let state: HandoffStateService;
  let http: HttpTestingController;
  const submitHandoffRequest = vi.fn().mockResolvedValue(undefined);
  const draftRequest = vi.fn();

  beforeEach(() => {
    submitHandoffRequest.mockClear();
    draftRequest.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FileUploadService, useValue: { listSessionFiles: async () => [] } },
        { provide: MessageMapService, useValue: { getMessagesForSession: () => signal([]) } },
        { provide: ChatRequestService, useValue: { submitHandoffRequest } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false, viewedSessionId: () => 'src' } },
        { provide: ComposerDraftService, useValue: { request: draftRequest } },
        {
          provide: ConversationBindingService,
          useValue: { queryParamsFor: (id: string) => ({ agent: id }), boundAgentId: () => 'course-agent' },
        },
        { provide: Router, useValue: { navigate: vi.fn() } },
        {
          provide: ToastService,
          useValue: { info: vi.fn(), success: vi.fn(), warning: vi.fn(), dismiss: vi.fn() },
        },
      ],
    });
    service = TestBed.inject(AgentHandoffService);
    state = TestBed.inject(HandoffStateService);
    http = TestBed.inject(HttpTestingController);
    state.upsertRow(CHIP);
  });

  afterEach(() => http.verify());

  async function start(): Promise<void> {
    const opened = service.startSuggestion(CHIP, { agentId: 'rubric', name: 'Rubric Builder' });
    const req = http.expectOne(`${API}/sessions/src/handoffs/s1/start`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ agentId: 'rubric' });
    req.flush(started());
    await opened;
  }

  it('Start opens the review sheet with the model’s prompt, for the picked Agent', async () => {
    await start();

    const review = state.reviewFor('src');
    expect(review?.phase).toBe('ready');
    expect(review?.request).toBe(PROMPT);
    expect(review?.card).toBe(CARD);
    expect(review?.draft).toMatchObject({
      agentId: 'rubric',
      agentName: 'Rubric Builder',
      suggestionId: 's1',
      sourceAssistantId: 'course-agent',
    });
    expect(submitHandoffRequest).not.toHaveBeenCalled();
  });

  it('cancelling a suggestion’s sheet leaves the chip: no dismiss, nothing in the composer', async () => {
    await start();

    service.cancel('src');

    expect(state.reviewFor('src')).toBeUndefined();
    http.expectNone(`${API}/sessions/src/handoffs/s1/dismiss`);
    expect(draftRequest).not.toHaveBeenCalled();
    expect(state.rowsFor('src')).toEqual([CHIP]);
  });

  it('a suggestion cancelled while its card loads is not dismissed either', async () => {
    const opened = service.startSuggestion(CHIP, { agentId: 'rubric', name: 'Rubric Builder' });
    service.cancel('src');
    http.expectOne(`${API}/sessions/src/handoffs/s1/start`).flush(started());
    await opened;

    http.expectNone(`${API}/sessions/src/handoffs/s1/dismiss`);
    expect(state.reviewFor('src')).toBeUndefined();
  });

  it('Send turns the chip into a running row of kind suggested, in the same place', async () => {
    await start();

    await service.send('src');

    const [row] = state.rowsFor('src');
    expect(row).toMatchObject({ handoffId: 's1', kind: 'suggested', status: 'running', sourceMessageIndex: 1 });
    expect(isSuggestionChip(row)).toBe(false);
    expect(submitHandoffRequest).toHaveBeenCalledWith(
      expect.objectContaining({ handoffId: 's1', agentId: 'rubric', message: PROMPT, contextText: CARD }),
    );
  });

  it('Dismiss removes the chip at once and tells the server', async () => {
    const dismissed = service.dismissSuggestion(CHIP);

    expect(state.rowsFor('src')[0].status).toBe('dismissed');
    const req = http.expectOne(`${API}/sessions/src/handoffs/s1/dismiss`);
    expect(req.request.method).toBe('POST');
    req.flush(null, { status: 204, statusText: 'No Content' });
    await dismissed;
  });

  it('Dismiss while its sheet is open closes the sheet too', async () => {
    await start();

    const dismissed = service.dismissSuggestion(CHIP);

    expect(state.reviewFor('src')).toBeUndefined();
    http.expectOne(`${API}/sessions/src/handoffs/s1/dismiss`).flush(null, { status: 204, statusText: 'No Content' });
    await dismissed;
    expect(draftRequest).not.toHaveBeenCalled();
  });
});
