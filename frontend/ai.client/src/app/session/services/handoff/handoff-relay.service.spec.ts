import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { HandoffView } from './handoff.models';
import { HandoffRelayService } from './handoff-relay.service';
import { HandoffStateService } from './handoff-state.service';

const API = 'https://api.test';
const EVENTS_URL = `${API}/sessions/src/handoffs/h1/notifications/events`;

function row(overrides: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId: 'h1',
    sessionId: 'src',
    kind: 'mention',
    status: 'needs_input',
    agentId: 'rubric',
    agentName: 'Rubric Builder',
    request: 'draft a rubric for this',
    sourceMessageIndex: 1,
    excerptTokens: 20,
    createdAt: '2026-11-04T00:00:00Z',
    updatedAt: '2026-11-04T00:00:00Z',
    targetSessionId: 'tgt',
    relayedPrompts: [{ interruptId: 'a1', kind: 'tool_approval', toolName: 'create_assignment' }],
    ...overrides,
  };
}

/** Which surface answered, and what §4.7 counts for each (PR-0.3c). */
describe('HandoffRelayService', () => {
  let http: HttpTestingController;
  let viewed: ReturnType<typeof signal<string | null>>;
  const toastInfo = vi.fn();
  const reload = vi.fn().mockResolvedValue(undefined);

  function setup(handoffs = true): HandoffRelayService {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { ...environment.features, handoffs } },
        { provide: ChatStateService, useValue: { viewedSessionId: () => viewed(), isSessionLoading: () => false } },
        { provide: MessageMapService, useValue: { reloadMessagesForSession: reload } },
        { provide: ToastService, useValue: { info: toastInfo } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    return TestBed.inject(HandoffRelayService);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    viewed = signal<string | null>('src');
  });

  afterEach(() => {
    http?.verify();
    TestBed.resetTestingModule();
  });

  it('an answer from the source is counted once it resumed, and the relay collapses to one line', () => {
    const relay = setup();
    TestBed.inject(HandoffStateService).upsertRow(row());

    const resume = relay.beginResume('tgt', 'tool_approval');
    http.expectNone(EVENTS_URL);
    relay.endResume(resume!, 'resumed');

    expect(http.expectOne(EVENTS_URL).request.body).toEqual({
      metric: 'RelayedPromptAnswered',
      interruptKind: 'tool_approval',
      surface: 'source',
    });
    expect(relay.notice('h1')).toEqual({ text: 'Answered · Rubric Builder resumed', tone: 'success' });
    expect(TestBed.inject(HandoffStateService).rowsFor('src')[0].relayedPrompts).toBeUndefined();
  });

  it('an answer in the target is counted before it resumes, from the target alone', () => {
    viewed.set('tgt');
    const relay = setup();
    // A tab that never loaded the source knows the target's origin, not the row.
    TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', {
      sessionId: 'src',
      handoffId: 'h1',
      sourceTitle: 'Course planning',
      agentName: 'Rubric Builder',
    });

    const resume = relay.beginResume('tgt', 'oauth');

    expect(http.expectOne(EVENTS_URL).request.body).toEqual({
      metric: 'RelayedPromptAnswered',
      interruptKind: 'oauth',
      surface: 'target',
    });
    relay.endResume(resume!, 'resumed');
    http.expectNone(EVENTS_URL);
  });

  it('losing the race in the target says "Already answered" and re-reads the thread', () => {
    viewed.set('tgt');
    const relay = setup();
    TestBed.inject(HandoffStateService).upsertRow(row());

    relay.endResume(relay.beginResume('tgt', 'user_question')!, 'conflict');

    const events = http.match(EVENTS_URL).map((r) => r.request.body);
    expect(events).toContainEqual({ metric: 'RelayedPromptConflict', interruptKind: 'user_question' });
    expect(toastInfo).toHaveBeenCalledWith('Already answered', 'Rubric Builder already has your answer.');
    expect(reload).toHaveBeenCalledWith('tgt');
    http.expectOne(`${API}/sessions/src/handoffs/h1`).flush(row({ status: 'running', relayedPrompts: undefined }));
  });

  it('is not a relay: an ordinary conversation, a handoff that is not waiting, or the flag off', () => {
    let relay = setup();
    expect(relay.beginResume('other', 'oauth')).toBeNull();
    TestBed.inject(HandoffStateService).upsertRow(row({ status: 'done', relayedPrompts: undefined }));
    expect(relay.beginResume('tgt', 'oauth')).toBeNull();
    TestBed.resetTestingModule();

    relay = setup(false);
    TestBed.inject(HandoffStateService).upsertRow(row());
    expect(relay.beginResume('tgt', 'tool_approval')).toBeNull();
  });
});
