import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';

import { AgentMentionService } from '../../../agents/services/agent-mention.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { HandoffView } from '../../services/handoff/handoff.models';
import { matchSuggestedAgent } from '../../services/handoff/suggested-agent';

let nextId = 0;

/**
 * A suggested handoff (handoffs spec §4.4): the model offered a separate conversation for a piece
 * of work, and this chip waits under the answer that offered it. Nothing has started.
 *
 * - **Start** opens the same review sheet a mid-thread `@`-mention does, with the model's prompt
 *   as the request. The Agent is the one the model named, matched against the Agents the user can
 *   mention (`matchSuggestedAgent`). When the model named none, or nothing matched, the chip asks
 *   the user to pick one first, and a user with no Agents at all is told so and sent to browse them.
 * - **Dismiss** removes the chip, here and on reload.
 * - While its sheet is open, the chip says so instead of offering Start again. Once sent, the
 *   chip is replaced by the handoff's row, in the same place.
 */
@Component({
  selector: 'app-handoff-suggestion-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroArrowTopRightOnSquare })],
  template: `
    <section
      [attr.aria-labelledby]="titleId"
      class="rounded-2xl border border-dashed border-gray-300 bg-white px-4 py-3 dark:border-gray-600 dark:bg-gray-900"
    >
      <div class="flex items-start gap-3">
        <ng-icon
          name="heroArrowTopRightOnSquare"
          class="mt-0.5 size-5 shrink-0 text-gray-500 dark:text-gray-400"
          aria-hidden="true"
        />
        <div class="min-w-0 flex-1">
          <p class="text-xs font-medium text-gray-600 dark:text-gray-400">Suggested handoff</p>
          <h3 [id]="titleId" class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ chip().title }}</h3>
          @if (chip().tldr) {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">{{ chip().tldr }}</p>
          }

          @if (inReview()) {
            <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400" role="status">Reviewing it below.</p>
          } @else if (agents().length === 0) {
            <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
              <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                You don’t have an Agent to hand this to yet.
                <a
                  routerLink="/agents"
                  class="rounded-sm font-medium text-primary-accessible underline hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
                  >Browse Agents</a
                >
              </p>
              <button
                type="button"
                (click)="dismiss()"
                class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10"
              >
                Dismiss
              </button>
            </div>
          } @else {
            <div class="mt-2 flex flex-wrap items-end gap-2">
              @if (!matched()) {
                <div class="min-w-0">
                  <label [attr.for]="selectId" class="block text-xs font-medium text-gray-700 dark:text-gray-300">
                    {{ chip().agentHint ? 'Pick an Agent (“' + chip().agentHint + '” isn’t one of yours)' : 'Pick an Agent' }}
                  </label>
                  <select
                    [id]="selectId"
                    class="mt-1 block w-full max-w-xs rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-hidden focus:ring-2 focus:ring-primary-500/40 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    [value]="pickedId()"
                    (change)="pick($event)"
                  >
                    <option value="">Choose…</option>
                    @for (agent of agents(); track agent.agentId) {
                      <option [value]="agent.agentId">{{ agent.name }}</option>
                    }
                  </select>
                </div>
              }
              <button
                type="button"
                (click)="start()"
                [disabled]="!agent() || starting()"
                class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {{ matched() ? 'Hand off to ' + matched()!.name : 'Start' }}
              </button>
              <button
                type="button"
                (click)="dismiss()"
                class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10"
              >
                Dismiss
              </button>
            </div>
          }
        </div>
      </div>
    </section>
  `,
})
export class HandoffSuggestionChipComponent {
  readonly chip = input.required<HandoffView>();

  private readonly handoff = inject(AgentHandoffService);
  private readonly state = inject(HandoffStateService);
  private readonly mentions = inject(AgentMentionService);

  protected readonly titleId = `handoff-suggestion-title-${nextId}`;
  protected readonly selectId = `handoff-suggestion-agent-${nextId++}`;

  protected readonly agents = this.mentions.mentionable;
  protected readonly matched = computed(() => matchSuggestedAgent(this.chip().agentHint, this.agents()));
  protected readonly pickedId = signal('');
  protected readonly starting = signal(false);

  /** The Agent Start will use: the matched one, or the one picked. */
  protected readonly agent = computed(
    () => this.matched() ?? this.agents().find((candidate) => candidate.agentId === this.pickedId()),
  );

  protected readonly inReview = computed(
    () => this.state.reviewFor(this.chip().sessionId)?.draft.suggestionId === this.chip().handoffId,
  );

  constructor() {
    // Cached for the session by the services behind it, so this is free after the first chip.
    void this.mentions.load();
  }

  protected pick(event: Event): void {
    this.pickedId.set((event.target as HTMLSelectElement).value);
  }

  protected async start(): Promise<void> {
    const agent = this.agent();
    if (!agent || this.starting()) return;
    this.starting.set(true);
    try {
      await this.handoff.startSuggestion(this.chip(), { agentId: agent.agentId, name: agent.name });
    } finally {
      this.starting.set(false);
    }
  }

  protected dismiss(): void {
    void this.handoff.dismissSuggestion(this.chip());
  }
}
