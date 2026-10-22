import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ConfigService } from '../../../services/config.service';
import { ComposerDraftService } from './composer-draft.service';
import { Message } from '../models/message.model';
import {
  MessageFeedbackService,
  parseMessageRef,
  readPersistedFeedback,
  retryTemplate,
} from './message-feedback.service';

function message(id: string, metadata: Record<string, unknown> | null = null): Message {
  return { id, role: 'assistant', content: [{ type: 'text', text: 'hi' }], metadata };
}

describe('message-feedback helpers', () => {
  it('parseMessageRef splits on the LAST dash so dashed session ids survive', () => {
    expect(parseMessageRef('msg-abc-def-7')).toEqual({ sessionId: 'abc-def', index: 7 });
    expect(parseMessageRef('msg-s-0')).toEqual({ sessionId: 's', index: 0 });
    expect(parseMessageRef('placeholder')).toBeNull();
    expect(parseMessageRef('msg-s-x')).toBeNull();
    expect(parseMessageRef('msg--1')).toBeNull();
  });

  it('readPersistedFeedback accepts only ±1 and known reason codes', () => {
    expect(readPersistedFeedback(message('m'))).toBeNull();
    expect(readPersistedFeedback(message('m', { feedback: { value: 2 } }))).toBeNull();
    expect(readPersistedFeedback(message('m', { feedback: { value: -1, reason: 'wrong', updatedAt: 't' } }))).toEqual({
      value: -1,
      reason: 'wrong',
      updatedAt: 't',
    });
    // An unknown reason is dropped rather than rendered — it cannot be a chip.
    expect(readPersistedFeedback(message('m', { feedback: { value: 1, reason: 'free text' } }))?.reason).toBeUndefined();
  });
});

describe('MessageFeedbackService', () => {
  let service: MessageFeedbackService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://api.test/') } },
      ],
    });
    service = TestBed.inject(MessageFeedbackService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('prefers what the server sent with the message until a click overrides it', () => {
    const m = message('msg-s-3', { feedback: { value: 1, updatedAt: 't' } });
    expect(service.feedbackFor(m)?.value).toBe(1);
  });

  it('PUTs a content-free body to the message index and keeps the optimistic value', async () => {
    const m = message('msg-s-3');
    const done = service.setFeedback(m, -1, 'tool_failed');
    expect(service.feedbackFor(m)?.value).toBe(-1);

    const req = http.expectOne('http://api.test/sessions/s/messages/3/feedback');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ value: -1, reason: 'tool_failed' });
    req.flush({ value: -1, reason: 'tool_failed', updatedAt: '2026-09-16T00:00:00Z' });
    await done;
    expect(service.feedbackFor(m)).toEqual({ value: -1, reason: 'tool_failed', updatedAt: '2026-09-16T00:00:00Z' });
  });

  it('rolls back to the last confirmed value when the write fails', async () => {
    const m = message('msg-s-3', { feedback: { value: 1, updatedAt: 't' } });
    const done = service.setFeedback(m, -1);
    expect(service.feedbackFor(m)?.value).toBe(-1);
    http.expectOne('http://api.test/sessions/s/messages/3/feedback').flush('nope', { status: 500, statusText: 'err' });
    await done;
    expect(service.feedbackFor(m)?.value).toBe(1);
    expect(service.unavailable()).toBe(false);
  });

  it('DELETE withdraws the thumb', async () => {
    const m = message('msg-s-3', { feedback: { value: 1, updatedAt: 't' } });
    const done = service.clearFeedback(m);
    expect(service.feedbackFor(m)).toBeNull();
    const req = http.expectOne('http://api.test/sessions/s/messages/3/feedback');
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });
    await done;
    expect(service.feedbackFor(m)).toBeNull();
  });

  it('marks the surface unavailable only on the kill-switch 404', async () => {
    const m = message('msg-s-3');
    let done = service.setFeedback(m, 1);
    http.expectOne('http://api.test/sessions/s/messages/3/feedback').flush({ detail: 'Session not found: s' }, { status: 404, statusText: 'nf' });
    await done;
    expect(service.unavailable()).toBe(false);

    done = service.setFeedback(m, 1);
    http.expectOne('http://api.test/sessions/s/messages/3/feedback').flush({ detail: 'Not found' }, { status: 404, statusText: 'nf' });
    await done;
    expect(service.unavailable()).toBe(true);
  });

  it('ignores messages without a server index', async () => {
    await service.setFeedback(message('placeholder'), 1);
    http.expectNone(() => true);
  });

  describe('implicit signals', () => {
    it('POSTs a kind code once per message per kind and never blocks on failure', async () => {
      const m = message('msg-s-3');
      service.recordSignal(m, 'copy');
      service.recordSignal(m, 'copy');
      service.recordSignal(m, 'continue');
      const posts = http.match('http://api.test/sessions/s/messages/3/signals');
      expect(posts.map((r) => r.request.body)).toEqual([{ kind: 'copy' }, { kind: 'continue' }]);
      posts[0].flush(null, { status: 204, statusText: 'No Content' });
      posts[1].flush('nope', { status: 500, statusText: 'err' });
      await Promise.resolve();
      expect(service.unavailable()).toBe(false);
    });

    it('ignores messages without a server index and stops once unavailable', () => {
      service.recordSignal(message('placeholder'), 'copy');
      service.unavailable.set(true);
      service.recordSignal(message('msg-s-3'), 'copy');
      http.expectNone(() => true);
    });
  });

  describe('retry with correction', () => {
    it('requestRetry drafts the reason\'s template into that session\'s composer', () => {
      const drafts = TestBed.inject(ComposerDraftService);
      const m = message('msg-s-3', { feedback: { value: -1, reason: 'instructions', updatedAt: 't' } });
      service.requestRetry(m);
      const draft = drafts.pending();
      expect(draft?.sessionId).toBe('s');
      expect(draft?.text).toBe(retryTemplate('instructions'));
      expect(draft?.text).toContain('ignored my instructions');
      http.expectNone(() => true);
    });

    it('links the sent correction to the thumb as retryMessageId, an index only', async () => {
      const m = message('msg-s-3', { feedback: { value: -1, reason: 'wrong', updatedAt: 't' } });
      service.requestRetry(m);
      service.consumePendingRetry('s', message('msg-s-4'));
      const req = http.expectOne('http://api.test/sessions/s/messages/3/feedback');
      expect(req.request.method).toBe('PUT');
      expect(req.request.body).toEqual({ value: -1, reason: 'wrong', retryMessageId: 4 });
      req.flush({ value: -1, reason: 'wrong', retryMessageId: 4, updatedAt: 'u' });
      await Promise.resolve();
      expect(service.feedbackFor(m)?.retryMessageId).toBe(4);
      // Consumed: a second send does not link again.
      service.consumePendingRetry('s', message('msg-s-6'));
      http.expectNone(() => true);
    });

    it('a message on another session leaves the retry pending; a withdrawn thumb drops it', () => {
      const m = message('msg-s-3', { feedback: { value: -1, updatedAt: 't' } });
      service.requestRetry(m);
      service.consumePendingRetry('other', message('msg-other-1'));
      http.expectNone(() => true);
      service.consumePendingRetry('s', message('msg-s-4'));
      http.expectOne('http://api.test/sessions/s/messages/3/feedback').flush({ value: -1, retryMessageId: 4, updatedAt: 'u' });
    });

    it('every reason has a template and none is empty', () => {
      for (const reason of ['wrong', 'instructions', 'length', 'tool_failed', 'outdated', 'other'] as const) {
        expect(retryTemplate(reason).length).toBeGreaterThan(10);
      }
      expect(retryTemplate(undefined)).toBe(retryTemplate('other'));
    });
  });
});
