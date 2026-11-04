import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { NotificationKind } from '../../../services/notifications/notification.model';
import { NotificationsService } from '../../../services/notifications/notifications.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ConversationBindingService, bindingQueryParams } from '../session/conversation-binding.service';
import { SessionService } from '../session/session.service';
import { HandoffStatus, HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

/** The Runtime writes the inbox entry just after the row moves; wait this long before acting on it. */
export const INBOX_WRITE_GRACE_MS = 1500;

const OFFER_DISMISSED_KEY = 'handoff-browser-notification-offer-dismissed';

const KIND_FOR: Partial<Record<HandoffStatus, NotificationKind>> = {
  needs_input: 'handoff_needs_input',
  done: 'handoff_done',
  stopped: 'handoff_stopped',
};

const ALL_KINDS: NotificationKind[] = ['handoff_needs_input', 'handoff_done', 'handoff_stopped'];

export type BrowserPermission = NotificationPermission | 'unsupported';

type NotificationEvent =
  | { metric: 'HandoffNotified' | 'HandoffNotificationOpened'; channel: 'inbox' | 'browser' }
  | { metric: 'HandoffBrowserPermission'; outcome: 'granted' | 'denied' | 'dismissed' };

/** What a handoff's minimal routing needs: a bell entry's payload carries the same ids. */
export interface HandoffRef {
  handoffId: string;
  sourceSessionId: string;
  targetSessionId: string;
  agentId: string;
  /** The entry's kind, when it came from the bell. */
  kind?: NotificationKind;
  /** What a `handoff_needs_input` entry waits on (`WaitingOn.kind`). */
  waitingKind?: string;
}

/**
 * Whether a handoff's prompt can be answered from its source (PR-0.3c): it waits on the user, on
 * something other than a browser sign-in. The row's relayed prompts decide when the SPA has them
 * (none at all means nothing there to answer); otherwise `waitingOn`.
 */
export function answeredInSource(row: Pick<HandoffView, 'status' | 'waitingOn' | 'relayedPrompts'>): boolean {
  if (row.status !== 'needs_input') return false;
  if (row.relayedPrompts) return row.relayedPrompts.some((prompt) => prompt.kind !== 'browser_login');
  return !!row.waitingOn && row.waitingOn.kind !== 'browser_login';
}

/**
 * Handoff notifications in the SPA (handoffs spec §4.3 "Notifications", PR-0.3b).
 *
 * The Runtime writes the inbox entry (`handoff_needs_input` / `handoff_done` / `handoff_stopped`).
 * This service owns the two things only the tab can do:
 *
 * - **Read state follows the work, not the bell.** Opening a target marks its entries read. A
 *   state change that lands while the user is looking at the source or the target is marked read
 *   as soon as the entry exists, so the bell counts only what they missed.
 * - **Browser notifications** (the Notification API, no service worker), only while the page is
 *   hidden, tagged with the handoff id so a later state replaces an earlier one. The text is
 *   generic ("*Agent* needs your input"), because it can show on a lock screen. Permission is
 *   asked only when the user presses the in-context offer or the setting; never on load.
 * - **A click opens where the answer is** (PR-0.3c). For a prompt the source relays, the bell
 *   entry and the browser notification open the **source**, scrolled to the handoff row, so the
 *   user answers without losing their place. Anything else (a browser sign-in, a finished or
 *   stopped handoff) opens the target.
 */
@Injectable({ providedIn: 'root' })
export class HandoffNotificationService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly inbox = inject(NotificationsService);
  private readonly chatState = inject(ChatStateService);
  private readonly state = inject(HandoffStateService);
  private readonly router = inject(Router);
  private readonly binding = inject(ConversationBindingService);
  private readonly sessions = inject(SessionService);
  private readonly features = inject(FEATURES);

  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  private readonly permissionState = signal<BrowserPermission>(readPermission());
  private readonly offerDismissed = signal(readFlag(OFFER_DISMISSED_KEY));

  /** Handoffs whose state changed in this tab while the user was elsewhere: their entries are unread. */
  private readonly missed = new Set<string>();
  /** The last `target:handoff` pair opened, so one visit marks once. */
  private lastOpened: string | null = null;

  readonly permission = this.permissionState.asReadonly();

  /** Offer "Get a browser notification next time": supported, never answered, not waved away. */
  readonly canOffer = computed(
    () => this.features.handoffs && this.permissionState() === 'default' && !this.offerDismissed(),
  );

  constructor() {
    if (!this.features.handoffs) return;
    effect(() => {
      const viewed = this.chatState.viewedSessionId();
      const spawned = this.state.spawnedFrom(viewed);
      untracked(() => {
        if (viewed && spawned) this.onTargetOpened(viewed, spawned.sessionId, spawned.handoffId);
        else this.lastOpened = null;
      });
    });
  }

  /**
   * A handoff's turn ended in `row.status` (the status the Runtime wrote). Call once per change,
   * from wherever the SPA learns it.
   */
  onStateChange(row: HandoffView): void {
    if (!this.features.handoffs) return;
    const kind = KIND_FOR[row.status];
    const target = row.targetSessionId;
    if (!kind || !target || row.stoppedReason === 'user_stopped') return;
    // A row the Runtime never took has no inbox entry; only the tab knows.
    const inInbox = row.stoppedReason !== 'not_started';
    const visible = isVisible();
    const viewed = this.chatState.viewedSessionId();
    if (visible && (viewed === row.sessionId || viewed === target)) {
      if (inInbox) void this.markInView(row.sessionId, row.handoffId, kind);
      return;
    }
    if (inInbox) {
      this.missed.add(row.handoffId);
      setTimeout(() => void this.inbox.refresh(), INBOX_WRITE_GRACE_MS);
    }
    if (!visible) this.showBrowserNotification(row);
  }

  /** Mark a handoff's entries read (the bell's own click, Bring back result, opening the target). */
  async markRead(sourceSessionId: string, handoffId: string, kinds: NotificationKind[] = ALL_KINDS): Promise<number> {
    let marked = 0;
    try {
      const resp = await firstValueFrom(
        this.http.post<{ marked: number }>(
          `${this.handoffUrl(sourceSessionId, handoffId)}/notifications/read`,
          { kinds },
          this.options,
        ),
      );
      marked = resp.marked;
    } catch {
      return 0;
    }
    this.missed.delete(handoffId);
    this.inbox.markedElsewhere(
      (n) => n.payload?.handoffId === handoffId && kinds.includes(n.kind) && !n.readAt,
      marked,
    );
    return marked;
  }

  /** A bell entry for a handoff was opened: count it and go where the answer is. */
  openFromInbox(ref: HandoffRef): void {
    this.record(ref.sourceSessionId, ref.handoffId, { metric: 'HandoffNotificationOpened', channel: 'inbox' });
    if (this.inboxEntryAnsweredInSource(ref)) this.openSource(ref);
    else this.openTarget(ref);
  }

  /**
   * Open the source thread with the handoff's row in view (the relayed prompt sits under it). The
   * row takes the focus request once it renders, so this works from any page, and in place when
   * the source is already open.
   */
  openSource(ref: Pick<HandoffRef, 'handoffId' | 'sourceSessionId'>): void {
    this.state.requestFocus(ref.handoffId);
    if (this.chatState.viewedSessionId() === ref.sourceSessionId) return;
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === ref.sourceSessionId);
    const { project, assistantId } = bindingQueryParams(
      listed?.preferences?.assistantId,
      listed?.preferences?.projectId,
      this.features.projects,
    );
    void this.router.navigate(['/s', ref.sourceSessionId], {
      queryParams: project ? { project } : assistantId ? { assistantId } : {},
    });
  }

  /** Ask for browser-notification permission. Only ever from a click: the offer or the setting. */
  async requestPermission(row?: Pick<HandoffView, 'sessionId' | 'handoffId'>): Promise<BrowserPermission> {
    if (this.permissionState() === 'unsupported') return 'unsupported';
    let result: NotificationPermission;
    try {
      result = await Notification.requestPermission();
    } catch {
      return this.permissionState();
    }
    this.permissionState.set(result);
    if (row) {
      const outcome = result === 'default' ? 'dismissed' : result;
      this.record(row.sessionId, row.handoffId, { metric: 'HandoffBrowserPermission', outcome });
    }
    return result;
  }

  /** "No thanks" on the offer. The setting can still turn them on. */
  dismissOffer(): void {
    this.offerDismissed.set(true);
    writeFlag(OFFER_DISMISSED_KEY);
  }

  private onTargetOpened(targetSessionId: string, sourceSessionId: string, handoffId: string): void {
    const key = `${targetSessionId}:${handoffId}`;
    if (this.lastOpened === key) return;
    this.lastOpened = key;
    // Nothing unread here, and nothing this tab saw land: no request.
    if (this.inbox.unreadCount() === 0 && !this.missed.has(handoffId)) return;
    void this.markRead(sourceSessionId, handoffId);
  }

  /** The entry is written a moment after the row moves; look once, then once more. */
  private async markInView(sourceSessionId: string, handoffId: string, kind: NotificationKind): Promise<void> {
    await delay(INBOX_WRITE_GRACE_MS);
    if ((await this.markRead(sourceSessionId, handoffId, [kind])) > 0) return;
    await delay(INBOX_WRITE_GRACE_MS * 2);
    await this.markRead(sourceSessionId, handoffId, [kind]);
  }

  private showBrowserNotification(row: HandoffView): void {
    const target = row.targetSessionId;
    if (this.permissionState() !== 'granted' || !target) return;
    let notification: Notification;
    try {
      notification = new Notification(notificationTitle(row), notificationOptions(row));
    } catch {
      // Some browsers (Chrome on Android) only allow notifications from a service worker.
      return;
    }
    notification.onclick = () => {
      window.focus();
      notification.close();
      this.record(row.sessionId, row.handoffId, { metric: 'HandoffNotificationOpened', channel: 'browser' });
      if (answeredInSource(row)) {
        this.openSource({ handoffId: row.handoffId, sourceSessionId: row.sessionId });
        return;
      }
      this.openTarget({
        handoffId: row.handoffId,
        sourceSessionId: row.sessionId,
        targetSessionId: target,
        agentId: row.agentId,
      });
    };
    this.record(row.sessionId, row.handoffId, { metric: 'HandoffNotified', channel: 'browser' });
  }

  /**
   * A `handoff_needs_input` entry names what it waits on. An entry written before it did reads the
   * row this tab holds, and otherwise opens the target, where any prompt can be answered.
   */
  private inboxEntryAnsweredInSource(ref: HandoffRef): boolean {
    if (ref.kind !== undefined && ref.kind !== 'handoff_needs_input') return false;
    if (ref.waitingKind) return ref.waitingKind !== 'browser_login';
    const row = this.state.rowsFor(ref.sourceSessionId).find((r) => r.handoffId === ref.handoffId);
    return !!row && answeredInSource(row);
  }

  private openTarget(ref: HandoffRef): void {
    void this.router.navigate(['/s', ref.targetSessionId], {
      queryParams: this.binding.queryParamsFor(ref.agentId),
    });
  }

  private record(sourceSessionId: string, handoffId: string, event: NotificationEvent): void {
    this.http
      .post<void>(`${this.handoffUrl(sourceSessionId, handoffId)}/notifications/events`, event, this.options)
      .subscribe({ error: () => undefined });
  }

  private handoffUrl(sourceSessionId: string, handoffId: string): string {
    return `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sourceSessionId)}/handoffs/${encodeURIComponent(handoffId)}`;
  }
}

/**
 * The app's own square icon, from `public/favicon` (served at the root), so a deployment that
 * replaces its favicons gets its own icon here too. On macOS the sender badge stays the browser's:
 * the OS credits a notification to the app that posted it, unless the site is installed as an app.
 */
export const NOTIFICATION_ICON = '/android-chrome-192x192.png';

/** `NotificationOptions` plus `renotify`, which Chromium implements and lib.dom no longer types. */
export type HandoffNotificationOptions = NotificationOptions & { renotify?: boolean };

/**
 * One notification per handoff (`tag`). A later state replaces the earlier one, and `renotify`
 * makes that replacement alert again (sound, banner), because a replacement is silent by default
 * and "finished" after "needs your input" is news. Whether it plays a sound is the OS's setting.
 */
export function notificationOptions(row: Pick<HandoffView, 'handoffId'>): HandoffNotificationOptions {
  return { tag: row.handoffId, icon: NOTIFICATION_ICON, renotify: true };
}

/** The one line a notification says. Never request or conversation text. */
export function notificationTitle(row: Pick<HandoffView, 'agentName' | 'status'>): string {
  switch (row.status) {
    case 'needs_input':
      return `${row.agentName} needs your input`;
    case 'done':
      return `${row.agentName} finished`;
    default:
      return `${row.agentName} stopped`;
  }
}

function readPermission(): BrowserPermission {
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission;
}

function isVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible';
}

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === 'true';
  } catch {
    return false;
  }
}

function writeFlag(key: string): void {
  try {
    localStorage.setItem(key, 'true');
  } catch {
    // Private mode: the offer comes back next visit, which is harmless.
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
