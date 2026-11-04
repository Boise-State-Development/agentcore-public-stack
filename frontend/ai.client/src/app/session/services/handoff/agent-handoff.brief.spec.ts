import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { FEATURES, FeatureFlags } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { environment } from '../../../../environments/environment';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { AgentHandoffService } from './agent-handoff.service';
import { HandoffBriefHandlers, HandoffBriefService } from './handoff-brief.service';
import { wrapHandoffContext } from './handoff-prompt';
import { CreateHandoffResponse } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

const API = 'https://api.test';
const EXCERPT = '<conversation_excerpt turns="1-2" of="2">\nUser: plan BIO 101\n</conversation_excerpt>';
const BODY = { message: 'draft a rubric', session_id: 'src', handoff_brief: { handoffId: 'h1' } };

function created(): CreateHandoffResponse {
  const contextBlock = wrapHandoffContext(EXCERPT, 'Course planning');
  return {
    handoffId: 'h1',
    excerpt: EXCERPT,
    excerptHash: 'hash',
    excerptTokens: 20,
    turnsCovered: { first: 1, last: 2, of: 2, omitted: 0 },
    contextBlock,
    prompt: `${contextBlock}\n\ndraft a rubric`,
    request: 'draft a rubric',
    sourceMessageIndex: 3,
    sourceTitle: 'Course planning',
  };
}

/** The handoff brief (handoffs spec §4.5.1, PR-0.3a): what the review sheet's state does with its stream. */
describe('AgentHandoffService — brief', () => {
  let service: AgentHandoffService;
  let state: HandoffStateService;
  let http: HttpTestingController;
  let handlers: HandoffBriefHandlers | undefined;
  let closeStream: () => void;
  const submitHandoffRequest = vi.fn().mockResolvedValue(undefined);
  const buildHandoffBriefRequest = vi.fn().mockResolvedValue(BODY);
  const stream = vi.fn();
  const preempt = vi.fn().mockResolvedValue(undefined);

  function setup(handoffBrief: boolean): void {
    submitHandoffRequest.mockClear();
    buildHandoffBriefRequest.mockClear();
    preempt.mockClear();
    handlers = undefined;
    stream.mockReset().mockImplementation((_src: string, _id: string, _body: unknown, h: HandoffBriefHandlers) => {
      handlers = h;
      return new Promise<void>((resolve) => (closeStream = resolve));
    });
    const features: FeatureFlags = { ...environment.features, handoffs: true, handoffBrief };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: FEATURES, useValue: features },
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: ChatRequestService, useValue: { submitHandoffRequest, buildHandoffBriefRequest } },
        { provide: HandoffBriefService, useValue: { stream, preempt, isRunning: () => false } },
        { provide: ChatStateService, useValue: { isSessionLoading: () => false, viewedSessionId: () => 'src' } },
        { provide: ComposerDraftService, useValue: { request: vi.fn() } },
        { provide: ConversationBindingService, useValue: { queryParamsFor: (id: string) => ({ agent: id }) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
        { provide: ToastService, useValue: { info: vi.fn(), success: vi.fn(), warning: vi.fn() } },
      ],
    });
    service = TestBed.inject(AgentHandoffService);
    state = TestBed.inject(HandoffStateService);
    http = TestBed.inject(HttpTestingController);
  }

  afterEach(() => http.verify());

  async function open(): Promise<void> {
    const opened = service.open('src', {
      agentId: 'rubric',
      agentName: 'Rubric Builder',
      request: 'draft a rubric',
      sourceAssistantId: 'tutor',
    });
    http.expectOne(`${API}/sessions/src/handoffs`).flush(created());
    await opened;
    // Let the brief request's awaits (preempt, body) settle.
    await vi.waitFor(() => expect(stream).toHaveBeenCalled());
  }

  const review = () => state.reviewFor('src')!;

  it('with the flag off there is no brief request: the card is the excerpt, as before', async () => {
    setup(false);
    const opened = service.open('src', { agentId: 'rubric', agentName: 'Rubric Builder', request: 'draft a rubric' });
    http.expectOne(`${API}/sessions/src/handoffs`).flush(created());
    await opened;
    await Promise.resolve();

    expect(buildHandoffBriefRequest).not.toHaveBeenCalled();
    expect(stream).not.toHaveBeenCalled();
    expect(review().card).toBe(EXCERPT);
    expect(review().brief).toBeUndefined();
  });

  it('requests the brief on the source with the turn body and the source binding', async () => {
    setup(true);
    await open();

    expect(buildHandoffBriefRequest).toHaveBeenCalledWith({
      sourceSessionId: 'src',
      handoffId: 'h1',
      request: 'draft a rubric',
      sourceAssistantId: 'tutor',
    });
    expect(stream).toHaveBeenCalledWith('src', 'h1', BODY, expect.anything());
    expect(review().brief).toEqual({ status: 'writing', text: '' });
    expect(review().card).toBe(EXCERPT);
  });

  it('streams text into the brief, and a written brief becomes the card that is sent', async () => {
    setup(true);
    await open();

    handlers!.onText('## Goal\n');
    handlers!.onText('A rubric for BIO 101.\n');
    expect(review().brief?.text).toBe('## Goal\nA rubric for BIO 101.\n');
    expect(review().card).toBe(EXCERPT);

    // Send waits for the brief to settle: what the user reviewed is what goes.
    await service.send('src');
    expect(submitHandoffRequest).not.toHaveBeenCalled();

    handlers!.onDone({ briefHash: 'bh', cacheStatus: 'hit' });
    closeStream();
    expect(review().brief).toEqual({ status: 'written', text: '## Goal\nA rubric for BIO 101.', briefHash: 'bh', cacheStatus: 'hit' });
    expect(review().card).toBe('## Goal\nA rubric for BIO 101.');
    expect(service.previewPrompt(review())).toBe(
      `${wrapHandoffContext('## Goal\nA rubric for BIO 101.', 'Course planning')}\n\ndraft a rubric`,
    );

    // Edits apply to the brief like any card.
    service.setCard('src', '## Goal\nA rubric for BIO 101, four rows.');
    await service.send('src');
    expect(submitHandoffRequest.mock.calls[0][0]).toMatchObject({
      handoffId: 'h1',
      contextText: '## Goal\nA rubric for BIO 101, four rows.',
    });
  });

  it('a fallback discards the streamed text and keeps the excerpt', async () => {
    setup(true);
    await open();

    handlers!.onText('## Goal\nhalf a bri');
    handlers!.onFallback('timeout');
    closeStream();

    expect(review().brief).toEqual({ status: 'fallback', text: '', fallbackReason: 'timeout' });
    expect(review().card).toBe(EXCERPT);

    await service.send('src');
    expect(submitHandoffRequest.mock.calls[0][0].contextText).toBe(EXCERPT);
  });

  it('frames from a brief whose sheet was cancelled change nothing, and the cancel stops it cancel-only', async () => {
    setup(true);
    await open();
    preempt.mockClear();

    service.cancel('src');
    http.expectOne(`${API}/sessions/src/handoffs/h1/dismiss`).flush(null);
    expect(preempt).toHaveBeenCalledWith('src');

    handlers!.onText('late text');
    handlers!.onDone({ briefHash: 'bh' });
    expect(state.reviewFor('src')).toBeUndefined();
  });

  it('"Use the excerpt" settles at once and stops the brief', async () => {
    setup(true);
    await open();
    preempt.mockClear();
    handlers!.onText('partial');

    service.useExcerpt('src');

    expect(preempt).toHaveBeenCalledWith('src');
    expect(review().brief).toEqual({ status: 'fallback', text: '', fallbackReason: 'preempted' });
    expect(review().card).toBe(EXCERPT);
    // The stream's own fallback when it closes is ignored: already settled.
    handlers!.onFallback('preempted');
    handlers!.onText('more');
    expect(review().brief?.text).toBe('');
  });

  it('a body that cannot be built falls back rather than leaving the sheet writing', async () => {
    setup(true);
    buildHandoffBriefRequest.mockRejectedValueOnce(new Error('No model selected.'));
    const opened = service.open('src', { agentId: 'rubric', agentName: 'Rubric Builder', request: 'draft a rubric' });
    http.expectOne(`${API}/sessions/src/handoffs`).flush(created());
    await opened;
    await vi.waitFor(() => expect(review().brief?.status).toBe('fallback'));

    expect(review().brief?.fallbackReason).toBe('error');
    expect(stream).not.toHaveBeenCalled();
  });
});
