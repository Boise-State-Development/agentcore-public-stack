import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { NotificationsService } from '../../../services/notifications/notifications.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ConversationBindingService } from '../session/conversation-binding.service';
import { SessionService } from '../session/session.service';
import { HandoffView } from './handoff.models';
import { HandoffNotificationService, INBOX_WRITE_GRACE_MS, notificationTitle } from './handoff-notification.service';
import { HandoffStateService } from './handoff-state.service';

const API = 'https://api.test';
const READ_URL = `${API}/sessions/src/handoffs/h1/notifications/read`;
const EVENTS_URL = `${API}/sessions/src/handoffs/h1/notifications/events`;

function row(overrides: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId: 'h1',
    sessionId: 'src',
    kind: 'mention',
    status: 'done',
    agentId: 'rubric',
    agentName: 'Rubric Builder',
    request: 'draft a rubric for this',
    sourceMessageIndex: 1,
    excerptTokens: 20,
    createdAt: '2026-11-04T00:00:00Z',
    updatedAt: '2026-11-04T00:00:00Z',
    targetSessionId: 'tgt',
    ...overrides,
  };
}

/** A stand-in for the browser's Notification API: records what was shown. */
class FakeNotification {
  static permission: NotificationPermission = 'default';
  static instances: FakeNotification[] = [];
  static requestPermission = vi.fn(async () => FakeNotification.permission);
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(
    readonly title: string,
    readonly options: NotificationOptions,
  ) {
    FakeNotification.instances.push(this);
  }
}

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
}

describe('HandoffNotificationService', () => {
  let http: HttpTestingController;
  let viewed: ReturnType<typeof signal<string | null>>;
  const navigate = vi.fn().mockResolvedValue(true);
  const inbox = {
    unreadCount: signal(0),
    refresh: vi.fn().mockResolvedValue(undefined),
    markedElsewhere: vi.fn(),
  };

  function setup(handoffs = true): HandoffNotificationService {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { ...environment.features, handoffs } },
        { provide: NotificationsService, useValue: inbox },
        { provide: ChatStateService, useValue: { viewedSessionId: () => viewed() } },
        { provide: ConversationBindingService, useValue: { queryParamsFor: (id: string) => ({ agent: id }) } },
        { provide: Router, useValue: { navigate } },
        {
          provide: SessionService,
          useValue: {
            mergedSessionsResource: () => ({
              sessions: [{ sessionId: 'src', preferences: { assistantId: 'planner' } }],
            }),
          },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    return TestBed.inject(HandoffNotificationService);
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    viewed = signal<string | null>('elsewhere');
    inbox.unreadCount.set(0);
    FakeNotification.permission = 'default';
    FakeNotification.instances = [];
    vi.stubGlobal('Notification', FakeNotification);
    setVisibility('visible');
    try {
      localStorage.clear();
    } catch {
      /* no storage */
    }
  });

  afterEach(() => {
    http?.verify();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  describe('browser notifications', () => {
    it('fire only while the page is hidden, with generic text tagged by handoff', () => {
      FakeNotification.permission = 'granted';
      const service = setup();

      setVisibility('visible');
      service.onStateChange(row({ status: 'done' }));
      expect(FakeNotification.instances).toHaveLength(0);

      setVisibility('hidden');
      service.onStateChange(row({ status: 'needs_input' }));
      expect(FakeNotification.instances).toHaveLength(1);
      const [shown] = FakeNotification.instances;
      expect(shown.title).toBe('Rubric Builder needs your input');
      expect(shown.title).not.toContain('rubric for this');
      expect(shown.options).toEqual({ tag: 'h1' });
      http.expectOne((r) => r.url === EVENTS_URL && r.body.metric === 'HandoffNotified' && r.body.channel === 'browser');
    });

    it('never fire before permission is granted', () => {
      FakeNotification.permission = 'default';
      const service = setup();
      setVisibility('hidden');

      service.onStateChange(row({ status: 'needs_input' }));

      expect(FakeNotification.instances).toHaveLength(0);
      expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
    });

    it('replace an earlier state: every state of one handoff carries the same tag', () => {
      FakeNotification.permission = 'granted';
      const service = setup();
      setVisibility('hidden');

      service.onStateChange(row({ status: 'needs_input' }));
      service.onStateChange(row({ status: 'done' }));

      expect(FakeNotification.instances.map((n) => [n.title, n.options.tag])).toEqual([
        ['Rubric Builder needs your input', 'h1'],
        ['Rubric Builder finished', 'h1'],
      ]);
      http.match(EVENTS_URL);
    });

    it('open the target on click and count the open', () => {
      FakeNotification.permission = 'granted';
      const focus = vi.spyOn(window, 'focus').mockImplementation(() => undefined);
      const service = setup();
      setVisibility('hidden');
      service.onStateChange(row({ status: 'done' }));
      http.match(EVENTS_URL);

      FakeNotification.instances[0].onclick?.();

      expect(focus).toHaveBeenCalled();
      expect(FakeNotification.instances[0].close).toHaveBeenCalled();
      expect(navigate).toHaveBeenCalledWith(['/s', 'tgt'], { queryParams: { agent: 'rubric' } });
      const opened = http.expectOne(EVENTS_URL);
      expect(opened.request.body).toEqual({ metric: 'HandoffNotificationOpened', channel: 'browser' });
    });

    it('stay quiet for a Stop the user pressed', () => {
      FakeNotification.permission = 'granted';
      const service = setup();
      setVisibility('hidden');

      service.onStateChange(row({ status: 'stopped', stoppedReason: 'user_stopped' }));

      expect(FakeNotification.instances).toHaveLength(0);
    });

    it('do nothing at all with the flag off', () => {
      FakeNotification.permission = 'granted';
      const service = setup(false);
      setVisibility('hidden');

      service.onStateChange(row({ status: 'needs_input' }));

      expect(FakeNotification.instances).toHaveLength(0);
      expect(service.canOffer()).toBe(false);
    });
  });

  describe('permission', () => {
    it('is never asked on its own: only when the offer or the setting is pressed', async () => {
      const service = setup();
      setVisibility('hidden');
      service.onStateChange(row({ status: 'needs_input' }));
      expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
      expect(service.canOffer()).toBe(true);

      FakeNotification.permission = 'granted';
      await service.requestPermission(row());

      expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
      expect(service.permission()).toBe('granted');
      expect(service.canOffer()).toBe(false);
      const recorded = http.expectOne((r) => r.url === EVENTS_URL && r.body.metric === 'HandoffBrowserPermission');
      expect(recorded.request.body.outcome).toBe('granted');
    });

    it('records a closed prompt as dismissed and keeps offering', async () => {
      const service = setup();
      FakeNotification.permission = 'default';

      await service.requestPermission(row());

      expect(http.expectOne(EVENTS_URL).request.body).toEqual({ metric: 'HandoffBrowserPermission', outcome: 'dismissed' });
      expect(service.canOffer()).toBe(true);
    });

    it('"No thanks" stops the offer', () => {
      const service = setup();
      service.dismissOffer();
      expect(service.canOffer()).toBe(false);
    });

    it('is not offered where the browser has no Notification API', () => {
      vi.stubGlobal('Notification', undefined);
      const service = setup();
      expect(service.permission()).toBe('unsupported');
      expect(service.canOffer()).toBe(false);
    });
  });

  describe('read state', () => {
    it('marks a change that lands while the source is in view, once the entry exists', async () => {
      viewed.set('src');
      const service = setup();

      service.onStateChange(row({ status: 'done' }));
      http.expectNone(READ_URL);
      await vi.advanceTimersByTimeAsync(INBOX_WRITE_GRACE_MS);

      const req = http.expectOne(READ_URL);
      expect(req.request.body).toEqual({ kinds: ['handoff_done'] });
      req.flush({ marked: 1 });
      await vi.advanceTimersByTimeAsync(INBOX_WRITE_GRACE_MS * 3);
      http.expectNone(READ_URL);
      expect(inbox.markedElsewhere).toHaveBeenCalledWith(expect.any(Function), 1);
      expect(inbox.refresh).not.toHaveBeenCalled();
    });

    it('looks once more when the entry was not written yet', async () => {
      viewed.set('tgt');
      const service = setup();

      service.onStateChange(row({ status: 'needs_input' }));
      await vi.advanceTimersByTimeAsync(INBOX_WRITE_GRACE_MS);
      http.expectOne(READ_URL).flush({ marked: 0 });
      await vi.advanceTimersByTimeAsync(INBOX_WRITE_GRACE_MS * 2);

      expect(http.expectOne(READ_URL).request.body).toEqual({ kinds: ['handoff_needs_input'] });
    });

    it('leaves a change unread when the user is elsewhere, and refreshes the bell to show it', async () => {
      viewed.set('another-conversation');
      const service = setup();

      service.onStateChange(row({ status: 'done' }));
      await vi.advanceTimersByTimeAsync(INBOX_WRITE_GRACE_MS * 4);

      http.expectNone(READ_URL);
      expect(inbox.refresh).toHaveBeenCalledTimes(1);
    });

    it('opening the target marks every entry of its handoff read', async () => {
      inbox.unreadCount.set(2);
      viewed.set('elsewhere');
      setup();
      const state = TestBed.inject(HandoffStateService);
      state.setSpawnedFrom('tgt', { sessionId: 'src', handoffId: 'h1', sourceTitle: 'Course planning', agentName: 'Rubric Builder' });
      TestBed.tick();
      http.expectNone(READ_URL);

      viewed.set('tgt');
      TestBed.tick();

      const req = http.expectOne(READ_URL);
      expect(req.request.body).toEqual({ kinds: ['handoff_needs_input', 'handoff_done', 'handoff_stopped'] });
      req.flush({ marked: 2 });
      await vi.advanceTimersByTimeAsync(0);
      // Staying on the target marks nothing again.
      TestBed.tick();
      http.expectNone(READ_URL);
    });

    it('opening a target with nothing unread makes no request', () => {
      inbox.unreadCount.set(0);
      viewed.set('tgt');
      setup();
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', {
        sessionId: 'src', handoffId: 'h1', sourceTitle: 'Course planning', agentName: 'Rubric Builder',
      });
      TestBed.tick();
      http.expectNone(READ_URL);
    });
  });

  /**
   * Notifications point where the answer is (§4.3, PR-0.3c): a prompt the source relays opens the
   * source with the handoff row in view; a browser sign-in, and every other state, the target.
   */
  describe('where a click goes', () => {
    const ref = { handoffId: 'h1', sourceSessionId: 'src', targetSessionId: 'tgt', agentId: 'rubric' };

    it.each(['oauth_consent', 'tool_approval', 'user_question'])(
      'a needs-input entry waiting on %s opens the source at the row',
      (waitingKind) => {
        const service = setup();
        service.openFromInbox({ ...ref, kind: 'handoff_needs_input', waitingKind });

        expect(navigate).toHaveBeenCalledWith(['/s', 'src'], { queryParams: { assistantId: 'planner' } });
        expect(TestBed.inject(HandoffStateService).focusedHandoffId()).toBe('h1');
        expect(http.expectOne(EVENTS_URL).request.body).toEqual({ metric: 'HandoffNotificationOpened', channel: 'inbox' });
      },
    );

    it('a browser sign-in opens the target, where its live view is', () => {
      const service = setup();
      service.openFromInbox({ ...ref, kind: 'handoff_needs_input', waitingKind: 'browser_login' });

      expect(navigate).toHaveBeenCalledWith(['/s', 'tgt'], { queryParams: { agent: 'rubric' } });
      expect(TestBed.inject(HandoffStateService).focusedHandoffId()).toBeNull();
      http.expectOne(EVENTS_URL);
    });

    it('a finished or stopped handoff opens the target', () => {
      const service = setup();
      service.openFromInbox({ ...ref, kind: 'handoff_done' });
      service.openFromInbox({ ...ref, kind: 'handoff_stopped' });

      expect(navigate.mock.calls.map((c) => c[0])).toEqual([['/s', 'tgt'], ['/s', 'tgt']]);
      http.match(EVENTS_URL);
    });

    it('an entry written before it named what it waits on reads the row this tab holds', () => {
      const service = setup();
      TestBed.inject(HandoffStateService).upsertRow(
        row({ status: 'needs_input', relayedPrompts: [{ interruptId: 'a1', kind: 'tool_approval', toolName: 't' }] }),
      );
      service.openFromInbox({ ...ref, kind: 'handoff_needs_input' });

      expect(navigate).toHaveBeenCalledWith(['/s', 'src'], { queryParams: { assistantId: 'planner' } });
      http.expectOne(EVENTS_URL);
    });

    it('opening the source while it is in view only brings the row into view', () => {
      viewed.set('src');
      const service = setup();
      service.openSource(ref);

      expect(navigate).not.toHaveBeenCalled();
      expect(TestBed.inject(HandoffStateService).focusedHandoffId()).toBe('h1');
    });

    it('a browser notification for a relayed prompt opens the source; for a sign-in, the target', () => {
      FakeNotification.permission = 'granted';
      vi.spyOn(window, 'focus').mockImplementation(() => undefined);
      const service = setup();
      setVisibility('hidden');
      service.onStateChange(
        row({ status: 'needs_input', relayedPrompts: [{ interruptId: 'i1', kind: 'oauth', providerId: 'canvas' }] }),
      );
      service.onStateChange(
        row({ handoffId: 'h2', status: 'needs_input', relayedPrompts: [{ interruptId: 'b1', kind: 'browser_login' }] }),
      );
      http.match(() => true);

      FakeNotification.instances[0].onclick?.();
      FakeNotification.instances[1].onclick?.();

      expect(navigate.mock.calls.map((c) => c[0])).toEqual([['/s', 'src'], ['/s', 'tgt']]);
      http.match(() => true);
    });
  });

  it('words each state without request text', () => {
    expect(notificationTitle({ agentName: 'Rubric Builder', status: 'needs_input' })).toBe('Rubric Builder needs your input');
    expect(notificationTitle({ agentName: 'Rubric Builder', status: 'done' })).toBe('Rubric Builder finished');
    expect(notificationTitle({ agentName: 'Rubric Builder', status: 'stopped' })).toBe('Rubric Builder stopped');
  });
});
