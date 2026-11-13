import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  untracked,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheckCircle, heroChevronRight, heroExclamationCircle, heroMinusCircle } from '@ng-icons/heroicons/outline';

import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffNotificationService } from '../../services/handoff/handoff-notification.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { DeliveryService } from '../../services/handoff/delivery.service';
import { HandoffStatus, HandoffView, WaitingOn } from '../../services/handoff/handoff.models';
import { SessionService } from '../../services/session/session.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { HandoffRelayComponent } from './handoff-relay.component';

/** The row's state as the user sees it: live while the target streams in this tab, persisted otherwise. */
type RowState = Extract<HandoffStatus, 'running' | 'needs_input' | 'done' | 'stopped'>;

/**
 * The handoff row (handoffs spec §4.3): a card in the source thread that is itself the link to the
 * Agent's conversation, the way Claude Code shows a session it started.
 *
 * - **Title:** the target's title, read from the same session list the sidenav shows, so a rename
 *   changes both. Until `session_title` lands, the request.
 * - **Status line:** the Agent's name, then what it is doing. While the target streams in this tab
 *   it follows the SPA's live state (latest tool summary, then `agent_status`, then the request);
 *   after that, the persisted `status` with `statusLine` / `waitingOn`.
 * - **One link, one tab stop.** The status line is not a live region: announcing every tool batch
 *   would talk over the thread being read. State changes are announced once, by the toast.
 * - **The browser-notification offer** (PR-0.3b) sits under a row that needs input, as sibling
 *   buttons, never inside the link, until the user answers it once (here, in the toast or in
 *   Settings) or waves it away.
 * - **The relayed prompt** (PR-0.3c) sits under the row too: the target's prompt, answered here
 *   without leaving the source (`HandoffRelayComponent`).
 * - **Focus on request.** A notification for a prompt the source can answer opens the source and
 *   asks for this row; it scrolls into view and takes focus once it renders.
 */
@Component({
  selector: 'app-handoff-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, HandoffRelayComponent],
  providers: [provideIcons({ heroCheckCircle, heroChevronRight, heroExclamationCircle, heroMinusCircle })],
  template: `
    @if (link(); as l) {
      <a
        #card
        [routerLink]="l.commands"
        [queryParams]="l.queryParams"
        [attr.aria-label]="accessibleName()"
        class="group flex items-center gap-3 rounded-2xl border border-gray-200 bg-white px-4 py-3 text-left transition-colors hover:border-gray-300 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-900 dark:hover:border-gray-600 dark:hover:bg-white/5"
      >
        <span class="flex size-5 shrink-0 items-center justify-center" aria-hidden="true">
          @switch (state()) {
            @case ('running') {
              <span class="relative flex size-2.5">
                <span class="absolute inline-flex size-full animate-ping rounded-full bg-primary-500 opacity-60 motion-reduce:animate-none"></span>
                <span class="relative inline-flex size-2.5 rounded-full bg-primary-500"></span>
              </span>
            }
            @case ('needs_input') {
              <ng-icon name="heroExclamationCircle" class="size-5 text-state-warning-700 dark:text-state-warning-400" />
            }
            @case ('done') {
              <ng-icon name="heroCheckCircle" class="size-5 text-state-success-600 dark:text-state-success-400" />
            }
            @default {
              <ng-icon name="heroMinusCircle" class="size-5 text-gray-400 dark:text-gray-500" />
            }
          }
        </span>
        <span class="min-w-0 flex-1">
          <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ title() }}</span>
          <span class="block truncate text-xs text-gray-600 dark:text-gray-400">
            {{ row().agentName }} · {{ statusLine() }}
          </span>
        </span>
        <ng-icon
          name="heroChevronRight"
          class="size-4 shrink-0 text-gray-400 transition-transform group-hover:translate-x-0.5 dark:text-gray-500"
          aria-hidden="true"
        />
      </a>
      <!-- Bring back result (§4.3): a sibling of the link, never inside it, on a finished row. -->
      @if (offerBringBack()) {
        <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 px-4 text-xs/5">
          @if (delivery.wasBroughtBack(row().handoffId)) {
            <span class="flex items-center gap-1 text-gray-600 dark:text-gray-400" role="status">
              <ng-icon name="heroCheckCircle" class="size-3.5" aria-hidden="true" />
              Added to your next message
            </span>
          } @else {
            <button
              type="button"
              (click)="bringBack()"
              [disabled]="delivery.isBringingBack(row().handoffId)"
              class="rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-wait disabled:opacity-60 dark:text-primary-50"
            >
              {{ delivery.isBringingBack(row().handoffId) ? 'Bringing back…' : 'Bring back result' }}
            </button>
          }
        </div>
      }
      @if (offerNotifications()) {
        <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 px-4 text-xs/5">
          <button
            type="button"
            (click)="enableNotifications()"
            class="rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
          >
            Get a browser notification next time
          </button>
          <button
            type="button"
            (click)="notifications.dismissOffer()"
            class="rounded-md text-gray-600 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400"
          >
            No thanks
          </button>
        </div>
      }
      <app-handoff-relay [row]="row()" />
    }
  `,
})
export class HandoffRowComponent {
  private readonly handoff = inject(AgentHandoffService);
  private readonly chatState = inject(ChatStateService);
  private readonly insight = inject(ToolInsightService);
  private readonly sessions = inject(SessionService);
  protected readonly notifications = inject(HandoffNotificationService);
  private readonly handoffState = inject(HandoffStateService);
  protected readonly delivery = inject(DeliveryService);
  private readonly connectors = inject(UserConnectorsService);
  private readonly card = viewChild<ElementRef<HTMLAnchorElement>>('card');

  readonly row = input.required<HandoffView>();

  constructor() {
    // A row waiting on a connection names the connector the way its consent card does, from the
    // connector list. The card loads it too, but the row can show without one (a source mid-turn).
    effect(() => {
      if (this.state() !== 'needs_input' || this.row().waitingOn?.kind !== 'oauth_consent') return;
      untracked(() => this.connectors.ensureLoaded());
    });

    effect(() => {
      const handoffId = this.row().handoffId;
      if (this.handoffState.focusedHandoffId() !== handoffId) return;
      const card = this.card()?.nativeElement;
      if (!card) return;
      untracked(() => {
        this.handoffState.clearFocus(handoffId);
        bringIntoView(card);
      });
    });
  }

  protected readonly link = computed(() => this.handoff.targetLink(this.row()));

  private readonly target = computed(() => this.row().targetSessionId ?? '');

  protected readonly state = computed<RowState>(() => {
    if (this.chatState.isSessionLoading(this.target())) return 'running';
    const status = this.row().status;
    return status === 'needs_input' || status === 'done' || status === 'stopped' ? status : 'running';
  });

  protected readonly title = computed(() => {
    const target = this.target();
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === target);
    const title = listed?.title?.trim();
    if (title && title !== 'New Conversation') return title;
    return truncate(this.row().request, 80);
  });

  protected readonly statusLine = computed(() => {
    const row = this.row();
    switch (this.state()) {
      case 'running': {
        const summary = this.insight.latestSummary(this.target());
        if (summary) return summary;
        const status = this.insight.status(this.target());
        if (status?.phase === 'tool_start' && status.toolName) return `Using ${status.toolName}`;
        if (status?.phase === 'thinking') return 'Thinking';
        return `Working on “${truncate(row.request, 60)}”`;
      }
      case 'needs_input':
        return waitingLine(row.waitingOn, (providerId) => this.connectors.displayNameFor(providerId));
      case 'done': {
        const summary = this.insight.latestSummary(this.target()) ?? row.statusLine;
        return summary ? `Finished · ${summary}` : 'Finished';
      }
      default:
        if (row.stoppedReason === 'connection_lost') return 'Stopped · connection lost';
        if (row.stoppedReason === 'not_started') return 'Didn’t start · open it to see why';
        return 'Stopped';
    }
  });

  /** A finished handoff can send its answer back here (the target is the user's own by now). */
  protected readonly offerBringBack = computed(
    () => this.state() === 'done' && !!this.row().targetSessionId && this.row().kind !== 'send_back',
  );

  protected bringBack(): void {
    void this.delivery.bringBack(this.row());
  }

  protected readonly offerNotifications = computed(
    () => this.state() === 'needs_input' && this.notifications.canOffer(),
  );

  protected enableNotifications(): void {
    void this.notifications.requestPermission(this.row());
  }

  protected readonly accessibleName = computed(
    () => `${this.title()}, ${this.row().agentName}, ${this.statusLine()}`,
  );
}

/** Scroll the card to the middle of the thread and focus it without a second jump. */
function bringIntoView(card: HTMLElement): void {
  if (typeof card.scrollIntoView === 'function') card.scrollIntoView({ block: 'center', behavior: 'smooth' });
  card.focus({ preventScroll: true });
}

function waitingLine(waiting: WaitingOn | undefined, providerName: (providerId: string) => string): string {
  switch (waiting?.kind) {
    case 'oauth_consent':
      return waiting.name ? `Waiting for you to connect ${providerName(waiting.name)}` : 'Waiting for you to connect an account';
    case 'tool_approval':
      return waiting.name ? `Waiting for your approval to run ${waiting.name}` : 'Waiting for your approval';
    case 'user_question':
      return waiting.name || 'Waiting for your answer';
    case 'browser_login':
      return 'Waiting for you to sign in';
    default:
      return 'Needs your input';
  }
}

function truncate(value: string, max: number): string {
  const text = value.trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
