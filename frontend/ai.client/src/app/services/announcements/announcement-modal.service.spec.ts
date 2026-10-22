import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { NavigationEnd, Router } from '@angular/router';
import { Subject } from 'rxjs';
import { AnnouncementsService } from './announcements.service';
import { Announcement } from './announcement.model';
import { AnnouncementModalService } from './announcement-modal.service';
import { SessionService } from '../../auth/session.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { ToolApprovalService } from '../tool-approval/tool-approval.service';
import { OAuthConsentService } from '../oauth-consent/oauth-consent.service';
import { McpAppConsentService } from '../../session/services/mcp-apps/mcp-app-consent.service';
import { MINIMAL_CHROME } from '../../shared/utils/route-chrome';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Policy update',
    body_markdown: '## Policy',
    summary: null,
    surfaces: ['panel', 'modal'],
    severity: 'info',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-02-01T00:00:00Z',
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    is_unread: true,
    is_updated: false,
    ...overrides,
  };
}

describe('AnnouncementModalService (§D8 turn-safety gate)', () => {
  let modalItem: ReturnType<typeof signal<Announcement | null>>;
  let isLoadingSession: ReturnType<typeof signal<string | null>>;
  let toolApprovalPending: ReturnType<typeof signal<boolean>>;
  let oauthPending: ReturnType<typeof signal<boolean>>;
  let mcpAppPending: ReturnType<typeof signal<unknown[]>>;
  let isAuthenticated: ReturnType<typeof signal<boolean>>;
  let routeData: Record<string, unknown>;
  let events: Subject<NavigationEnd>;
  let open: ReturnType<typeof vi.fn>;
  let activeElement: unknown;

  beforeEach(() => {
    TestBed.resetTestingModule();
    modalItem = signal<Announcement | null>(null);
    isLoadingSession = signal<string | null>(null);
    toolApprovalPending = signal(false);
    oauthPending = signal(false);
    mcpAppPending = signal<unknown[]>([]);
    isAuthenticated = signal(true);
    routeData = {};
    events = new Subject<NavigationEnd>();
    activeElement = null;
    open = vi.fn(() => ({ closed: { subscribe: vi.fn() } }));

    // DI-token overrides rather than vi.mock, per house convention.
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsService, useValue: { modalItem, ack: vi.fn() } },
        { provide: Dialog, useValue: { open } },
        {
          provide: Router,
          useValue: {
            events,
            routerState: { snapshot: { root: { data: routeData, firstChild: null } } },
          },
        },
        {
          provide: DOCUMENT,
          useValue: {
            get activeElement() {
              return activeElement;
            },
          },
        },
        { provide: SessionService, useValue: { isAuthenticated } },
        { provide: MessageMapService, useValue: { isLoadingSession } },
        { provide: ToolApprovalService, useValue: { hasPending: toolApprovalPending } },
        { provide: OAuthConsentService, useValue: { hasPending: oauthPending } },
        { provide: McpAppConsentService, useValue: { pending: mcpAppPending } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  /** Instantiate the service and flush its effect. */
  function start() {
    const service = TestBed.inject(AnnouncementModalService);
    TestBed.tick();
    return service;
  }

  function navigate() {
    // A real instance, not a cast: the service filters with `instanceof
    // NavigationEnd`, so a plain object is silently dropped and every
    // navigation assertion below would pass without testing anything.
    events.next(new NavigationEnd(1, '/somewhere', '/somewhere'));
    TestBed.tick();
  }

  /** A focused composer textarea holding `value`. */
  function focusComposer(value: string) {
    activeElement = {
      value,
      closest: (selector: string) => (selector === 'app-chat-input' ? {} : null),
    };
  }

  it('opens the modal once the feed produces one and the route settles', () => {
    start();
    expect(open).not.toHaveBeenCalled();

    modalItem.set(makeAnnouncement());
    TestBed.tick();

    expect(open).toHaveBeenCalledTimes(1);
    const [, config] = open.mock.calls[0];
    expect(config.data.announcement.announcement_id).toBe('a1');
  });

  it('passes disableClose only for a requiresAck announcement', () => {
    start();
    modalItem.set(makeAnnouncement({ requires_ack: true }));
    TestBed.tick();

    expect(open.mock.calls[0][1].disableClose).toBe(true);
  });

  it('does not open twice for the same announcement', () => {
    start();
    modalItem.set(makeAnnouncement());
    TestBed.tick();
    expect(open).toHaveBeenCalledTimes(1);

    navigate();
    navigate();
    expect(open).toHaveBeenCalledTimes(1);
  });

  describe('refuses to interrupt', () => {
    it('while a stream is running', () => {
      isLoadingSession.set('session-1');
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('while a tool-approval prompt is pending', () => {
      toolApprovalPending.set(true);
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('while an OAuth consent is pending and the stream reads as idle', () => {
      // The #934 shape: `isLoading()` is FALSE while a turn is paused on an
      // interrupt, so a stream-only gate would throw a modal over the consent
      // dialog and steal its focus.
      isLoadingSession.set(null);
      oauthPending.set(true);
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('while an MCP App consent is pending', () => {
      mcpAppPending.set([{}]);
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('while the composer holds a draft', () => {
      focusComposer('half a thought');
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('on a minimal-chrome route', () => {
      routeData['chrome'] = MINIMAL_CHROME;
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });

    it('when the session is not authenticated', () => {
      isAuthenticated.set(false);
      start();
      modalItem.set(makeAnnouncement());
      TestBed.tick();
      expect(open).not.toHaveBeenCalled();
    });
  });

  it('opens for an empty but focused composer — focus alone is not a draft', () => {
    focusComposer('   ');
    start();
    modalItem.set(makeAnnouncement());
    TestBed.tick();
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('does NOT fire when the blocker merely clears mid-session (§D8)', () => {
    // The whole point of reading the gate inputs untracked. A stream ending is
    // not a route settle: firing here would put a dialog in front of someone
    // seconds after they finished a thought. It stays eligible instead.
    isLoadingSession.set('session-1');
    start();
    modalItem.set(makeAnnouncement());
    TestBed.tick();
    expect(open).not.toHaveBeenCalled();

    isLoadingSession.set(null);
    TestBed.tick();
    oauthPending.set(false);
    TestBed.tick();

    expect(open).not.toHaveBeenCalled();
  });

  describe('openFor — the user asked for it', () => {
    it('opens even when every §D8 gate would refuse an interruption', () => {
      // A click is not an interruption. The gate exists to stop us throwing a
      // dialog at someone mid-thought; here the user is the one asking.
      isLoadingSession.set('session-1');
      toolApprovalPending.set(true);
      focusComposer('half a thought');
      const service = start();

      service.openFor(makeAnnouncement(), 'banner');

      expect(open).toHaveBeenCalledTimes(1);
      const [, config] = open.mock.calls[0];
      expect(config.data.announcement.announcement_id).toBe('a1');
      expect(config.data.sourceSurface).toBe('banner');
    });

    it('defaults the surface to `modal` when no source is given', () => {
      const service = start();
      service.openFor(makeAnnouncement());
      expect(open.mock.calls[0][1].data.sourceSurface).toBe('modal');
    });

    it('keeps disableClose on a requiresAck announcement', () => {
      // Opening it yourself is not a way around the acknowledgement.
      const service = start();
      service.openFor(makeAnnouncement({ requires_ack: true }), 'banner');
      expect(open.mock.calls[0][1].disableClose).toBe(true);
    });

    it('refuses to stack a second dialog on an open one', () => {
      const service = start();
      service.openFor(makeAnnouncement(), 'banner');
      service.openFor(makeAnnouncement({ announcement_id: 'a2' }), 'banner');
      expect(open).toHaveBeenCalledTimes(1);
    });

    it('stops the §D8 effect re-opening what the user already read', () => {
      // Same announcement in both slots — without marking it shown, the user
      // would read it from the banner and then be interrupted by it on the
      // next navigation.
      const service = start();
      service.openFor(makeAnnouncement(), 'banner');
      expect(open).toHaveBeenCalledTimes(1);

      // Let the dialog close, so `openRef` is not what is holding it back.
      const onClosed = open.mock.results[0].value.closed.subscribe.mock
        .calls[0][0] as () => void;
      onClosed();

      modalItem.set(makeAnnouncement());
      TestBed.tick();
      navigate();

      expect(open).toHaveBeenCalledTimes(1);
    });
  });

  it('opens on the next settled navigation after a failed gate', () => {
    isLoadingSession.set('session-1');
    start();
    modalItem.set(makeAnnouncement());
    TestBed.tick();
    expect(open).not.toHaveBeenCalled();

    // The turn finished and the user moved to another page — a clean load.
    isLoadingSession.set(null);
    navigate();

    expect(open).toHaveBeenCalledTimes(1);
  });
});
