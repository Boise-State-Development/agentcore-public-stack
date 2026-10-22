import { TestBed } from '@angular/core/testing';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import { BrowserLoginService } from './browser-login.service';
import { ConfigService } from '../config.service';

const SIGNED_URL =
  'https://bedrock-agentcore.us-west-2.amazonaws.com/live?X-Amz-Signature=deadbeef';

function request(overrides: Record<string, unknown> = {}) {
  return {
    interruptId: 'v1:tool_call:tu-1:abc',
    toolUseId: 'tu-1',
    sessionId: 'conv-1',
    browserSessionId: 'bs-1',
    browserId: 'browser-abc',
    viewport: { width: 1280, height: 800 },
    ...overrides,
  };
}

describe('BrowserLoginService', () => {
  let service: BrowserLoginService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        // DI token over vi.mock, per the repo's testing convention.
        {
          provide: ConfigService,
          useValue: { appApiUrl: () => 'https://api.example.test' },
        },
      ],
    });
    service = TestBed.inject(BrowserLoginService);
    http = TestBed.inject(HttpTestingController);
  });

  describe('tracking prompts', () => {
    it('surfaces a prompt from the stream', () => {
      service.requestLogin(request());

      expect(service.hasPending()).toBe(true);
      expect(service.pending()[0].browserSessionId).toBe('bs-1');
    });

    it('ignores a re-emitted interrupt so a replay cannot resurrect it', async () => {
      service.setResumeHandler(() => {});
      service.requestLogin(request());
      await service.complete('v1:tool_call:tu-1:abc');

      service.requestLogin(request());

      expect(service.hasPending()).toBe(false);
    });

    it('keeps a genuinely new takeover distinct', () => {
      service.requestLogin(request());
      service.requestLogin(request({ interruptId: 'v1:tool_call:tu-2:def', toolUseId: 'tu-2' }));

      expect(service.pending()).toHaveLength(2);
    });
  });

  describe('the sign-in window', () => {
    it('treats a past deadline as lapsed', () => {
      const req = { ...request({ deadlineAt: '2020-01-01T00:00:00Z' }), receivedAt: 0 };

      expect(service.hasLapsed(req)).toBe(true);
    });

    it('treats a future deadline as open', () => {
      const future = new Date(Date.now() + 60_000).toISOString();
      const req = { ...request({ deadlineAt: future }), receivedAt: 0 };

      expect(service.hasLapsed(req)).toBe(false);
    });

    it('treats a missing or unreadable deadline as open', () => {
      // Hiding the viewer because of an unparseable timestamp is the worse
      // failure — the session may be perfectly alive.
      expect(service.hasLapsed({ ...request(), receivedAt: 0 })).toBe(false);
      expect(
        service.hasLapsed({ ...request({ deadlineAt: 'soon' }), receivedAt: 0 }),
      ).toBe(false);
    });
  });

  describe('minting a live view', () => {
    it('posts only the conversation id and returns the url', async () => {
      const promise = service.mintLiveView('conv-1');

      const req = http.expectOne(
        'https://api.example.test/sessions/conv-1/browser/live-view',
      );
      expect(req.request.method).toBe('POST');
      // The browser session is resolved server-side. Sending it from here
      // would let any signed-in user stream any browser in the account.
      expect(req.request.body).toEqual({});
      req.flush({
        url: SIGNED_URL,
        expiresAt: '2026-09-18T12:05:00+00:00',
        viewport: { width: 1280, height: 800 },
        controlState: 'user',
      });

      await expect(promise).resolves.toMatchObject({ url: SIGNED_URL });
    });

    it('escapes the conversation id in the path', async () => {
      const promise = service.mintLiveView('conv/../other');
      http
        .expectOne('https://api.example.test/sessions/conv%2F..%2Fother/browser/live-view')
        .flush({ url: SIGNED_URL, expiresAt: 't', viewport: { width: 1, height: 1 }, controlState: 'user' });
      await promise;
    });

    it('does not cache the url on the pending request', async () => {
      // A minted URL lives at most 300 seconds. Storing one would guarantee a
      // dead stream the second time the viewer opened.
      service.requestLogin(request());
      const promise = service.mintLiveView('conv-1');
      http.expectOne(() => true).flush({
        url: SIGNED_URL,
        expiresAt: 't',
        viewport: { width: 1280, height: 800 },
        controlState: 'user',
      });
      await promise;

      expect(JSON.stringify(service.pending()[0])).not.toContain('X-Amz-Signature');
    });
  });

  describe('resuming the turn', () => {
    it('posts an object, never null, on completion', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestLogin(request());

      await service.complete('v1:tool_call:tu-1:abc');

      // A null response would re-raise the interrupt forever.
      expect(handler).toHaveBeenCalledWith(
        'v1:tool_call:tu-1:abc',
        { completed: true },
        { sessionId: 'conv-1' },
      );
    });

    it('posts an object on skip too', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestLogin(request());

      await service.skip('v1:tool_call:tu-1:abc');

      expect(handler).toHaveBeenCalledWith(
        'v1:tool_call:tu-1:abc',
        { skipped: true },
        { sessionId: 'conv-1' },
      );
    });

    it('carries an optional note', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestLogin(request());

      await service.skip('v1:tool_call:tu-1:abc', 'no account for this vendor');

      expect(handler.mock.calls[0][1]).toEqual({
        skipped: true,
        note: 'no account for this vendor',
      });
    });

    it('drops the prompt before the handler runs, so a double click is harmless', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestLogin(request());

      await service.complete('v1:tool_call:tu-1:abc');
      await service.complete('v1:tool_call:tu-1:abc');

      expect(handler).toHaveBeenCalledTimes(1);
      expect(service.hasPending()).toBe(false);
    });

    it('survives a handler that throws', async () => {
      service.setResumeHandler(() => {
        throw new Error('network down');
      });
      service.requestLogin(request());

      await expect(service.complete('v1:tool_call:tu-1:abc')).resolves.toBeUndefined();
    });

    it('does nothing for an unknown interrupt', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);

      await service.complete('never-seen');

      expect(handler).not.toHaveBeenCalled();
    });
  });
});
