// A turn that pauses on the user ends its stream with one of four prompt
// events. Each marks the conversation as waiting on the user, viewed or not,
// so the session list can show it without refetching. A pre-flight
// `oauth_required` (no interruptId) paused nothing and must not.
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';

const ROW = { sessionId: 's1', lastMessageAt: '2026-01-01T00:00:00Z', awaitingInput: null };

const EVENTS: Array<[string, Record<string, unknown>, string]> = [
  [
    'oauth_required',
    { type: 'oauth_required', providerId: 'provider-fake', authorizationUrl: 'https://auth.example.test/authorize', interruptId: 'interrupt-fake-1' },
    'oauth',
  ],
  [
    'tool_approval_required',
    { type: 'tool_approval_required', interruptId: 'interrupt-fake-2', toolUseId: 'tooluse-fake-2', toolName: 'say_hello', message: 'Approve?' },
    'tool_approval',
  ],
  [
    'user_question_required',
    {
      type: 'user_question_required',
      interruptId: 'interrupt-fake-3',
      toolUseId: 'tooluse-fake-3',
      questions: [{ header: 'Format', question: 'Which format?', multiSelect: false, options: [{ label: 'PDF' }, { label: 'Doc' }] }],
    },
    'user_question',
  ],
  [
    'browser_login_required',
    {
      type: 'browser_login_required',
      interruptId: 'interrupt-fake-4',
      toolUseId: 'tooluse-fake-4',
      sessionId: 's1',
      browserSessionId: 'browser-session-fake',
      browserId: 'browser-fake',
      viewport: { width: 1280, height: 800 },
    },
    'browser_login',
  ],
];

describe('StreamParserService - awaiting input', () => {
  let service: StreamParserService;
  let chatState: ChatStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
    chatState = TestBed.inject(ChatStateService);
  });

  afterEach(() => TestBed.resetTestingModule());

  for (const [event, data, kind] of EVENTS) {
    it(`marks a background conversation paused on ${event}`, () => {
      chatState.setViewedSession('another-session');
      service.reset('s1');
      service.parseEventSourceMessage('s1', 'message_start', { role: 'assistant' });
      service.parseEventSourceMessage('s1', event, data);
      expect(chatState.awaitingInputFor(ROW)).toBe(kind);
      expect(chatState.awaitingInputFor({ ...ROW, sessionId: 's2' })).toBeNull();
    });
  }

  it('does not mark a pre-flight consent prompt, which paused nothing', () => {
    service.reset('s1');
    const [, data] = EVENTS[0];
    service.parseEventSourceMessage('s1', 'oauth_required', { ...data, interruptId: undefined });
    expect(chatState.awaitingInputFor(ROW)).toBeNull();
  });
});
