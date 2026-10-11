import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { DOCUMENT } from '@angular/common';
import { PLATFORM_ID } from '@angular/core';
import { SessionPrewarmService } from './session-prewarm.service';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';

const API = 'http://api.test';
const URL = `${API}/chat/prewarm`;

/** A document whose visibility the test controls. */
function fakeDocument(initial: DocumentVisibilityState = 'visible') {
  const listeners: Array<() => void> = [];
  const doc = {
    visibilityState: initial,
    addEventListener: (type: string, fn: () => void) => {
      if (type === 'visibilitychange') listeners.push(fn);
    },
    becomes(state: DocumentVisibilityState) {
      doc.visibilityState = state;
      listeners.forEach((fn) => fn());
    },
    listenerCount: () => listeners.length,
  };
  return doc;
}

describe('SessionPrewarmService', () => {
  let httpMock: HttpTestingController;
  let doc: ReturnType<typeof fakeDocument>;

  function setup(options: { enabled?: boolean; visibility?: DocumentVisibilityState; platform?: string } = {}) {
    TestBed.resetTestingModule();
    doc = fakeDocument(options.visibility ?? 'visible');
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { sessionPrewarm: options.enabled ?? true } },
        { provide: DOCUMENT, useValue: doc },
        { provide: PLATFORM_ID, useValue: options.platform ?? 'browser' },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
    return TestBed.inject(SessionPrewarmService);
  }

  afterEach(() => {
    httpMock.verify();
    vi.useRealTimers();
  });

  describe('warming now (the new-conversation page)', () => {
    it('posts the conversation id, with error toasts suppressed', () => {
      const service = setup();
      service.warm('conv-1');
      const req = httpMock.expectOne(URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
      req.flush({ status: 'accepted' }, { status: 202, statusText: 'Accepted' });
    });

    it('warms the same conversation once per window, then again after it', () => {
      vi.useFakeTimers();
      const service = setup();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      service.warm('conv-1');
      httpMock.expectNone(URL);

      vi.advanceTimersByTime(SessionPrewarmService.REWARM_AFTER_MS + 1);
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
    });

    it('swallows a failure: no throw, and nothing for the caller to handle', () => {
      const service = setup();
      expect(() => service.warm('conv-1')).not.toThrow();
      httpMock.expectOne(URL).flush({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
    });

    it('ignores an empty id', () => {
      const service = setup();
      service.warm(null);
      service.warm('');
      httpMock.expectNone(URL);
    });
  });

  describe('existing conversations: first input, not load', () => {
    it('arming sends nothing; the first input warms the conversation', () => {
      const service = setup();
      service.armOnTyping('conv-1');
      httpMock.expectNone(URL);

      service.noteTyping();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      req.flush({});
    });

    it('every later keystroke in the window is free', () => {
      const service = setup();
      service.armOnTyping('conv-1');
      service.noteTyping();
      httpMock.expectOne(URL).flush({});
      service.noteTyping();
      service.noteTyping();
      httpMock.expectNone(URL);
    });

    it('typing warms again after the window, keeping a long reply warm', () => {
      vi.useFakeTimers();
      const service = setup();
      service.armOnTyping('conv-1');
      service.noteTyping();
      httpMock.expectOne(URL).flush({});

      vi.advanceTimersByTime(SessionPrewarmService.REWARM_AFTER_MS + 1);
      service.noteTyping();
      httpMock.expectOne(URL).flush({});
    });

    it('a real turn counts as a warm, so a follow-up sends nothing', () => {
      const service = setup();
      service.noteTurn('conv-1');
      service.armOnTyping('conv-1');
      service.noteTyping();
      httpMock.expectNone(URL);
    });

    it('typing on a page that armed nothing never warms', () => {
      const service = setup();
      service.noteTyping();
      httpMock.expectNone(URL);
    });

    it('leaving the conversation disarms it', () => {
      const service = setup();
      service.armOnTyping('conv-1');
      service.clearCurrent();
      service.noteTyping();
      httpMock.expectNone(URL);
    });

    it('an armed conversation is not re-warmed by the tab becoming visible', () => {
      vi.useFakeTimers();
      const service = setup();
      service.warmNewConversation();
      httpMock.expectOne(URL).flush({});
      service.armOnTyping('conv-1');

      vi.advanceTimersByTime(SessionPrewarmService.REWARM_AFTER_MS + 1);
      doc.becomes('hidden');
      doc.becomes('visible');
      httpMock.expectNone(URL);
    });

    it('moving to a new conversation disarms the old one', () => {
      const service = setup();
      service.armOnTyping('conv-1');
      service.warmNewConversation();
      httpMock.expectOne(URL).flush({});
      service.noteTyping();
      httpMock.expectNone(URL);
    });
  });

  describe('visibility', () => {
    it('defers a background tab until the user looks at it', () => {
      const service = setup({ visibility: 'hidden' });
      service.warm('conv-1');
      httpMock.expectNone(URL);

      doc.becomes('visible');
      httpMock.expectOne(URL).flush({});
    });

    it('re-warms the open conversation when the tab returns after the window', () => {
      vi.useFakeTimers();
      const service = setup();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});

      doc.becomes('hidden');
      vi.advanceTimersByTime(SessionPrewarmService.REWARM_AFTER_MS + 1);
      doc.becomes('visible');
      httpMock.expectOne(URL).flush({});
    });

    it('does not re-warm a conversation the user has left', () => {
      vi.useFakeTimers();
      const service = setup();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      service.clearCurrent();

      vi.advanceTimersByTime(SessionPrewarmService.REWARM_AFTER_MS + 1);
      doc.becomes('visible');
      httpMock.expectNone(URL);
    });

    it('registers one listener however many conversations are opened', () => {
      const service = setup();
      service.warm('a');
      service.warm('b');
      httpMock.match(URL).forEach((r) => r.flush({}));
      expect(doc.listenerCount()).toBe(1);
    });
  });

  describe('new conversations', () => {
    it('warms a minted id and hands that same id to the first send', () => {
      const service = setup();
      service.warmNewConversation();
      const req = httpMock.expectOne(URL);
      const warmed = req.request.body.session_id as string;
      req.flush({});

      expect(warmed).toMatch(/^[0-9a-f-]{36}$/);
      expect(service.claimNewConversationId()).toBe(warmed);
      // Claimed once: the next new conversation gets a different id.
      expect(service.claimNewConversationId()).toBeNull();
    });

    it('keeps one pending id across repeat visits to the new-conversation page', () => {
      const service = setup();
      service.warmNewConversation();
      const first = httpMock.expectOne(URL);
      first.flush({});
      service.warmNewConversation();
      httpMock.expectNone(URL);
      expect(service.claimNewConversationId()).toBe(first.request.body.session_id);
    });
  });

  describe('switched off', () => {
    it('sends nothing and mints nothing while the flag is off', () => {
      const service = setup({ enabled: false });
      service.warm('conv-1');
      service.warmNewConversation();
      service.armOnTyping('conv-2');
      service.noteTyping();
      httpMock.expectNone(URL);
      expect(service.claimNewConversationId()).toBeNull();
    });

    it('does nothing outside the browser', () => {
      const service = setup({ platform: 'server' });
      service.warm('conv-1');
      service.warmNewConversation();
      httpMock.expectNone(URL);
      expect(service.claimNewConversationId()).toBeNull();
    });
  });
});
