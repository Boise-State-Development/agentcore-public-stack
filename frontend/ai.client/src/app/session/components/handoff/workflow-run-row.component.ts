import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, inject, input, untracked, viewChild } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroChevronRight,
  heroExclamationCircle,
  heroMinusCircle,
  heroPauseCircle,
} from '@ng-icons/heroicons/outline';

import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { HandoffView } from '../../services/handoff/handoff.models';
import { WorkflowRunQueueService } from '../../services/handoff/workflow-run-queue.service';
import { WorkflowRunResultService } from '../../services/handoff/workflow-run-result.service';
import { RunStepLine, RunStepState, runProgress } from '../../services/handoff/workflow-run-progress';
import { WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService, agentsLabel, canResume } from '../../services/handoff/workflow-run.service';
import { SessionService } from '../../services/session/session.service';
import { HandoffRelayComponent } from './handoff-relay.component';
import { bringIntoView, handoffStatusLine, truncate } from './handoff-status-line';

/** One step line as the template draws it. */
interface StepLineView {
  handoffId: string;
  agentName: string;
  state: RunStepState;
  statusLine: string;
  /** The step's conversation, once it has one. A queued or unstarted step links nowhere. */
  link: ReturnType<AgentHandoffService['targetLink']>;
  accessibleName: string;
  /** The row the relay reads, for a step with a conversation. */
  row: HandoffView | null;
}

/**
 * The bar's fill per state: non-text, so at least 3:1 against the card in both themes, measured in
 * the running SPA (spec §19 PR-W.4b). A step with nothing to show yet is an outline, not a pale
 * fill, because no pale gray clears 3:1 on white.
 */
const BAR_FILL: Record<RunStepState, string> = {
  done: 'bg-state-success-600 dark:bg-state-success-500',
  running: 'bg-primary-500 dark:bg-primary-300',
  needs_input: 'bg-state-warning-600 dark:bg-state-warning-500',
  stopped: 'bg-gray-500',
  queued: 'ring-1 ring-inset ring-gray-500',
  not_started: 'ring-1 ring-inset ring-gray-500',
};

/**
 * A workflow run's row in its source thread (docs/specs/workflow-runs.md §20.2, PR-W.4b).
 *
 * The handoff row's look, with one difference: the card is a `<section>`, not a link, and each step
 * line inside it is the link, in the handoff row's style in small: the same marks, the same status
 * line from the same sources (`handoffStatusLine`), one tab stop each. A queued step reads
 * "Queued" and links nowhere until it starts. A step's relayed prompt opens under its own line,
 * never behind a click, because a hidden prompt is the stall §8 is about.
 *
 * Under the card, as siblings and never inside a link: Start (steps a reload left unstarted),
 * "Bring back *k* results now" / "Bring back results", and Stop run, which asks no confirmation.
 *
 * Not a live region, as the handoff row isn't: the toasts announce changes. A notification that
 * asks for one of its steps (`HandoffStateService.requestFocus`) scrolls to that step's line.
 */
@Component({
  selector: 'app-workflow-run-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, NgTemplateOutlet, RouterLink, HandoffRelayComponent],
  providers: [provideIcons({ heroCheckCircle, heroChevronRight, heroExclamationCircle, heroMinusCircle, heroPauseCircle })],
  template: `
    <section
      #card
      tabindex="-1"
      [attr.aria-labelledby]="headingId()"
      [attr.data-run-id]="run().runId"
      class="rounded-2xl border border-gray-200 bg-white px-4 py-3 focus:outline-none dark:border-gray-700 dark:bg-gray-900"
    >
      <h3 [id]="headingId()" class="flex items-baseline justify-between gap-3 text-sm/6">
        <span class="min-w-0 truncate font-medium text-gray-900 dark:text-white">{{ title() }}</span>
        <span class="sr-only">, </span>
        @if (progress().needsInput > 0) {
          <span class="shrink-0 text-xs/5 font-medium text-state-warning-700 dark:text-state-warning-400">
            {{ progress().needsInput }} needs you
          </span>
        } @else {
          <span class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400">
            {{ progress().done }} of {{ progress().total }} finished
          </span>
        }
      </h3>

      <div class="mt-2 flex gap-1" aria-hidden="true" data-testid="run-bar">
        @for (line of lines(); track line.handoffId) {
          <span class="h-1.5 flex-1 rounded-full" [class]="fill(line.state)" [attr.data-state]="line.state"></span>
        }
      </div>

      <ul role="list" class="-mx-2 mt-2 flex flex-col">
        @for (line of lines(); track line.handoffId) {
          <li>
            @if (line.link; as l) {
              <a
                [routerLink]="l.commands"
                [queryParams]="l.queryParams"
                [attr.aria-label]="line.accessibleName"
                [attr.data-handoff-id]="line.handoffId"
                class="group flex items-center gap-3 rounded-lg px-2 py-1 text-left transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-primary-500 dark:hover:bg-white/5"
              >
                <ng-container *ngTemplateOutlet="mark; context: { $implicit: line.state }" />
                <span class="min-w-0 flex-1 truncate text-xs/5 text-gray-600 dark:text-gray-400">
                  <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ line.agentName }}</span>
                  · {{ line.statusLine }}
                </span>
                <ng-icon
                  name="heroChevronRight"
                  class="size-4 shrink-0 text-gray-400 transition-transform group-hover:translate-x-0.5 dark:text-gray-500"
                  aria-hidden="true"
                />
              </a>
            } @else {
              <div class="flex items-center gap-3 px-2 py-1" [attr.data-handoff-id]="line.handoffId">
                <ng-container *ngTemplateOutlet="mark; context: { $implicit: line.state }" />
                <span class="min-w-0 flex-1 truncate text-xs/5 text-gray-600 dark:text-gray-400">
                  <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ line.agentName }}</span>
                  · {{ line.statusLine }}
                </span>
              </div>
            }
            @if (line.row) {
              <div class="px-2">
                <app-handoff-relay [row]="line.row" />
              </div>
            }
          </li>
        }
      </ul>
    </section>

    @if (offerStart() || offerBringBack() || onComposer() || offerStop() || startElsewhere()) {
      <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 px-4 text-xs/5">
        @if (offerStart()) {
          <button type="button" (click)="start()" class="rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark">
            Start {{ notStartedLabel() }}
          </button>
        } @else if (startElsewhere()) {
          <span class="text-gray-600 dark:text-gray-400">These weren’t started. Mention the Agents again to start them.</span>
        }
        @if (onComposer()) {
          <span class="flex items-center gap-1 text-gray-600 dark:text-gray-400" role="status">
            <ng-icon name="heroCheckCircle" class="size-3.5" aria-hidden="true" />
            Added to your next message
          </span>
        } @else if (offerBringBack()) {
          <button
            type="button"
            (click)="bringBack()"
            [disabled]="collecting()"
            class="rounded-md font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-wait disabled:opacity-60 dark:text-primary-accessible-dark"
          >
            {{ collecting() ? 'Bringing back…' : bringBackLabel() }}
          </button>
        }
        @if (offerStop()) {
          <button type="button" (click)="stop()" class="rounded-md font-medium text-gray-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300">
            Stop run
          </button>
        }
      </div>
    }

    <ng-template #mark let-state>
      <span class="flex size-5 shrink-0 items-center justify-center" aria-hidden="true">
        @switch (state) {
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
          @case ('queued') {
            <span class="size-2.5 rounded-full ring-1 ring-inset ring-gray-500"></span>
          }
          @case ('not_started') {
            <ng-icon name="heroPauseCircle" class="size-5 text-gray-500 dark:text-gray-400" />
          }
          @default {
            <ng-icon name="heroMinusCircle" class="size-5 text-gray-400 dark:text-gray-500" />
          }
        }
      </span>
    </ng-template>
  `,
})
export class WorkflowRunRowComponent {
  private readonly handoff = inject(AgentHandoffService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly insight = inject(ToolInsightService);
  private readonly sessions = inject(SessionService);
  private readonly connectors = inject(UserConnectorsService);
  private readonly queue = inject(WorkflowRunQueueService);
  private readonly runs = inject(WorkflowRunService);
  private readonly results = inject(WorkflowRunResultService);
  private readonly card = viewChild<ElementRef<HTMLElement>>('card');

  readonly run = input.required<WorkflowRunView>();

  constructor() {
    // A step waiting on a connection names the connector the way its consent card does.
    effect(() => {
      if (!this.progress().lines.some((line) => line.state === 'needs_input' && line.row?.waitingOn?.kind === 'oauth_consent')) return;
      untracked(() => this.connectors.ensureLoaded());
    });

    // A notification (or the "finished" toast's Open) asked for one of this run's steps.
    effect(() => {
      const handoffId = this.handoffState.focusedHandoffId();
      const card = this.card()?.nativeElement;
      if (!handoffId || !card || !this.run().steps.some((step) => step.handoffId === handoffId)) return;
      untracked(() => {
        this.handoffState.clearFocus(handoffId);
        const line = [...card.querySelectorAll<HTMLElement>('a[data-handoff-id]')].find((a) => a.dataset['handoffId'] === handoffId);
        bringIntoView(line ?? card);
      });
    });
  }

  protected readonly progress = computed(() => {
    const run = this.run();
    return runProgress(run, this.handoffState.rowsFor(run.sessionId), {
      isLoading: (target) => this.chatState.isSessionLoading(target),
      isPending: (handoffId) => this.queue.isPending(run.runId, handoffId),
    });
  });

  protected readonly lines = computed<StepLineView[]>(() => this.progress().lines.map((line) => this.lineView(line)));

  protected readonly headingId = computed(() => `run-row-${this.run().runId}`);

  protected readonly title = computed(() => {
    const run = this.run();
    const total = this.progress().total;
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === run.sessionId);
    const source = listed?.title?.trim();
    return source && source !== 'New Conversation' ? `${source} · ${agentsLabel(total)}` : `Handed off to ${agentsLabel(total)}`;
  });

  private readonly notStarted = computed(() => this.progress().lines.filter((line) => line.state === 'not_started').length);

  protected readonly offerStart = computed(() => this.notStarted() > 0 && canResume(this.run()));
  /** An approved run from before the card was kept on its row: nothing here can start it. */
  protected readonly startElsewhere = computed(() => this.notStarted() > 0 && !canResume(this.run()));
  protected readonly notStartedLabel = computed(() => agentsLabel(this.notStarted()));

  protected readonly collecting = computed(() => this.results.isCollecting(this.run().runId));
  protected readonly onComposer = computed(() => this.results.isOnComposer(this.run()));

  /** Offered until the run has its one result: once it does, a later step comes back on its own. */
  protected readonly offerBringBack = computed(() => this.progress().done > 0 && !this.run().resultDeliveryId);

  protected readonly bringBackLabel = computed(() => {
    const { done, settled } = this.progress();
    if (settled) return 'Bring back results';
    return `Bring back ${done} ${done === 1 ? 'result' : 'results'} now`;
  });

  protected readonly offerStop = computed(() => this.run().status === 'approved' && !this.progress().settled);

  protected fill(state: RunStepState): string {
    return BAR_FILL[state];
  }

  protected start(): void {
    this.runs.resume(this.run());
  }

  protected bringBack(): void {
    void this.results.collect(this.run());
  }

  protected stop(): void {
    void this.runs.stop(this.run());
  }

  private lineView(line: RunStepLine): StepLineView {
    const base = { handoffId: line.step.handoffId, agentName: line.step.agentName, state: line.state };
    if (line.state === 'queued' || line.state === 'not_started' || !line.row?.targetSessionId) {
      const statusLine = line.state === 'queued' ? 'Queued' : line.state === 'not_started' ? 'Not started' : 'Didn’t start';
      return { ...base, statusLine, link: null, accessibleName: `${line.step.agentName}, ${statusLine}`, row: null };
    }
    const row = line.row;
    const rowState = line.state === 'running' || line.state === 'needs_input' || line.state === 'done' ? line.state : 'stopped';
    const statusLine = handoffStatusLine(row, rowState, this.insight, (providerId) => this.connectors.displayNameFor(providerId));
    const target = row.targetSessionId ?? '';
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === target)?.title?.trim();
    const title = listed && listed !== 'New Conversation' ? listed : truncate(row.request, 80);
    return {
      ...base,
      statusLine,
      link: this.handoff.targetLink(row),
      accessibleName: `${title}, ${row.agentName}, ${statusLine}`,
      row,
    };
  }
}
