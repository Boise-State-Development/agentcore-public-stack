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
import { ConversationBindingService } from '../session/conversation-binding.service';
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

  /** A bell entry for a handoff was opened: count it and go to the target. */
  openFromInbox(ref: HandoffRef): void {
    this.record(ref.sourceSessionId, ref.handoffId, { metric: 'HandoffNotificationOpened', channel: 'inbox' });
    this.openTarget(ref);
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
      notification = new Notification(notificationTitle(row), { tag: row.handoffId });
    } catch {
      // Some browsers (Chrome on Android) only allow notifications from a service worker.
      return;
    }
    notification.onclick = () => {
      window.focus();
      notification.close();
      this.record(row.sessionId, row.handoffId, { metric: 'HandoffNotificationOpened', channel: 'browser' });
      this.openTarget({
        handoffId: row.handoffId,
        sourceSessionId: row.sessionId,
        targetSessionId: target,
        agentId: row.agentId,
      });
    };
    this.record(row.sessionId, row.handoffId, { metric: 'HandoffNotified', channel: 'browser' });
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
