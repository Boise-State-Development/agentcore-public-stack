import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { DOCUMENT } from '@angular/common';
import { PLATFORM_ID } from '@angular/core';
import { SessionPrewarmService } from './session-prewarm.service';
import { TurnSelectionService } from '../chat/turn-selection.service';
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
  let selection: {
    awaitSources: ReturnType<typeof vi.fn>;
    sourcesLoaded: ReturnType<typeof vi.fn>;
    incognitoPending: ReturnType<typeof vi.fn>;
    build: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    selection = {
      awaitSources: vi.fn().mockResolvedValue(undefined),
      sourcesLoaded: vi.fn().mockReturnValue(true),
      incognitoPending: vi.fn().mockReturnValue(false),
      build: vi.fn().mockReturnValue({
        model_id: 'm-1',
        enabled_tools: ['t1'],
        provider: 'bedrock',
        invoked_skills: ['never'],
        message: 'never',
        file_upload_ids: ['never'],
        rag_assistant_id: 'agent-1',
      }),
    };
  });

  function setup(options: { enabled?: boolean; prepare?: boolean; visibility?: DocumentVisibilityState; platform?: string } = {}) {
    TestBed.resetTestingModule();
    doc = fakeDocument(options.visibility ?? 'visible');
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { sessionPrewarm: options.enabled ?? true, sessionPrewarmPrepare: options.prepare ?? false } },
        { provide: TurnSelectionService, useValue: selection },
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

  describe('warm age, for the turn-timing report', () => {
    it('is null until a warm call goes out, then counts from it', () => {
      vi.useFakeTimers();
      const service = setup();
      expect(service.warmAgeMs('conv-1')).toBeNull();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      vi.advanceTimersByTime(4000);
      expect(service.warmAgeMs('conv-1')).toBe(4000);
    });

    it('a turn is not a prewarm', () => {
      const service = setup();
      service.noteTurn('conv-1');
      expect(service.warmAgeMs('conv-1')).toBeNull();
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

  describe('prepare', () => {
    const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

    function prepared(options: { prepare?: boolean } = {}) {
      const service = setup({ prepare: options.prepare ?? true });
      service.setPrepareContext(() => ({ assistantId: 'agent-1' }));
      return service;
    }

    it('flag off: an existing conversation sends today\'s plain warm and never builds a selection', () => {
      const service = prepared({ prepare: false });
      service.armOnTyping('conv-1');
      service.noteTyping();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      expect(selection.build).not.toHaveBeenCalled();
      req.flush({});
    });

    it('flag off: a new conversation sends exactly one call across load and typing', () => {
      const service = prepared({ prepare: false });
      service.warm('conv-1');
      service.noteTyping();
      service.noteTyping();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      expect(selection.build).not.toHaveBeenCalled();
      req.flush({});
    });

    it('an existing conversation sends one prepare with the allowlisted selection, in place of the warm', async () => {
      const service = prepared();
      service.armOnTyping('conv-1');
      httpMock.expectNone(URL);

      service.noteTyping();
      await settle();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({
        session_id: 'conv-1',
        prepare: true,
        model_id: 'm-1',
        enabled_tools: ['t1'],
        provider: 'bedrock',
        rag_assistant_id: 'agent-1',
      });
      expect(selection.build).toHaveBeenCalledWith('conv-1', { assistantId: 'agent-1' });
      req.flush({});

      // Second input: nothing.
      service.noteTyping();
      await settle();
      httpMock.expectNone(URL);
    });

    it('a new conversation warms on load, prepares on the first input, then sends nothing', async () => {
      const service = prepared();
      service.warm('conv-1');
      const warm = httpMock.expectOne(URL);
      expect(warm.request.body).toEqual({ session_id: 'conv-1' });
      warm.flush({});

      service.noteTyping();
      await settle();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toMatchObject({ session_id: 'conv-1', prepare: true, model_id: 'm-1' });
      req.flush({});

      service.noteTyping();
      await settle();
      httpMock.expectNone(URL);
    });

    it('a plain warm does not suppress a later prepare, and a second prepare is suppressed', async () => {
      const service = prepared();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      service.noteTyping();
      await settle();
      httpMock.expectOne(URL).flush({});
      // Within the window, another prepare for the conversation goes nowhere.
      service.warm('conv-1');
      httpMock.expectNone(URL);
      service.noteTyping();
      await settle();
      httpMock.expectNone(URL);
    });

    it('a real turn counts as both stages', async () => {
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTurn('conv-1');
      service.noteTyping();
      await settle();
      httpMock.expectNone(URL);
    });

    it('a turn that starts while the sources load drops the prepare', async () => {
      let release: () => void = () => undefined;
      selection.awaitSources.mockReturnValue(new Promise<void>((resolve) => (release = resolve)));
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTyping();
      service.noteTurn('conv-1');
      release();
      await settle();
      httpMock.expectNone(URL);
    });

    it('waits for unloaded sources instead of skipping, and sends nothing before they land', async () => {
      let release: () => void = () => undefined;
      selection.awaitSources.mockReturnValue(new Promise<void>((resolve) => (release = resolve)));
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTyping();
      await settle();
      httpMock.expectNone(URL);
      expect(selection.build).not.toHaveBeenCalled();

      release();
      await settle();
      httpMock.expectOne(URL).flush({});
      expect(selection.awaitSources).toHaveBeenCalledTimes(1);
    });

    it('falls back to a plain warm when the sources never load', async () => {
      selection.sourcesLoaded.mockReturnValue(false);
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTyping();
      await settle();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      req.flush({});
    });

    it('falls back to a plain warm when the page cannot tell its binding', async () => {
      const service = prepared();
      service.setPrepareContext(() => null);
      service.armOnTyping('conv-1');
      service.noteTyping();
      await settle();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      req.flush({});
    });

    it('an @-mention in the composer skips the prepare; an existing conversation gets the plain warm', async () => {
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTyping({ mention: true });
      await settle();
      const req = httpMock.expectOne(URL);
      expect(req.request.body).toEqual({ session_id: 'conv-1' });
      expect(selection.build).not.toHaveBeenCalled();
      req.flush({});
    });

    it('keeps warmAgeMs as the earlier stage and reports preparedAgeMs from the prepare', async () => {
      vi.useFakeTimers({ toFake: ['Date'] });
      const service = prepared();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      vi.advanceTimersByTime(5000);
      service.noteTyping();
      await vi.waitFor(() => httpMock.expectOne(URL).flush({}));
      vi.advanceTimersByTime(2000);
      // The warm is the earlier stage: it is older than the prepare by the gap between them.
      const warmAge = service.warmAgeMs('conv-1') as number;
      const preparedAge = service.preparedAgeMs('conv-1') as number;
      expect(warmAge - preparedAge).toBeGreaterThanOrEqual(5000);
      expect(warmAge).toBeGreaterThanOrEqual(7000);
    });

    async function prepareWith(service: SessionPrewarmService, id: string, response: unknown) {
      service.armOnTyping(id);
      service.noteTyping();
      await settle();
      httpMock.expectOne(URL).flush(response as object);
    }

    it('remembers a treated and a holdout arm from the prepare response', async () => {
      const service = prepared();
      await prepareWith(service, 'conv-1', { status: 'accepted', prepareArm: 'treated' });
      await prepareWith(service, 'conv-2', { status: 'recently_warmed', prepareArm: 'holdout' });
      expect(service.prepareArm('conv-1')).toBe('treated');
      expect(service.prepareArm('conv-2')).toBe('holdout');
    });

    it('records no arm when it is absent, bogus or the response is malformed', async () => {
      const service = prepared();
      await prepareWith(service, 'a', { status: 'accepted' });
      await prepareWith(service, 'b', { status: 'accepted', prepareArm: 'maybe' });
      await prepareWith(service, 'c', { status: 'accepted', prepareArm: 3 });
      await prepareWith(service, 'd', null);
      for (const id of ['a', 'b', 'c', 'd', 'never']) expect(service.prepareArm(id)).toBeNull();
    });

    it('does not attribute a response that lands after the send began', async () => {
      const service = prepared();
      service.armOnTyping('conv-1');
      service.noteTyping();
      await settle();
      const req = httpMock.expectOne(URL);
      service.noteTurn('conv-1');
      req.flush({ status: 'accepted', prepareArm: 'treated' });
      expect(service.prepareArm('conv-1')).toBeNull();
    });

    it('a plain warm response never sets an arm, and a failed prepare leaves none', async () => {
      const service = prepared();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({ prepareArm: 'treated' });
      expect(service.prepareArm('conv-1')).toBeNull();
      service.noteTyping();
      await settle();
      httpMock.expectOne(URL).flush('boom', { status: 500, statusText: 'err' });
      expect(service.prepareArm('conv-1')).toBeNull();
    });

    it('flag off: no arm even if a response names one', () => {
      const service = prepared({ prepare: false });
      service.armOnTyping('conv-1');
      service.noteTyping();
      httpMock.expectOne(URL).flush({ prepareArm: 'treated' });
      expect(service.prepareArm('conv-1')).toBeNull();
    });

    it('preparedAgeMs is null for a conversation never prepared', () => {
      const service = prepared();
      service.warm('conv-1');
      httpMock.expectOne(URL).flush({});
      expect(service.preparedAgeMs('conv-1')).toBeNull();
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
