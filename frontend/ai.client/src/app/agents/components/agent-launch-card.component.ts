import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroCheck,
  heroCheckBadge,
  heroLockClosed,
  heroNoSymbol,
  heroPlus,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { Agent, AgentRunnability } from '../models/agent.model';
import { ListingPublisher } from '../models/store.model';
import { runnabilityMessage } from '../models/runnability';
import { AgentIconComponent } from './agent-icon.component';
import { AgentPinService } from '../services/agent-pin.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * Everything the card renders, in the shape the card renders it.
 *
 * A view model rather than the `Agent` record itself, because the Designer's preview pane
 * shows an agent the user is *still typing* — name and starters that have no saved record
 * behind them yet. Handing that surface an `Agent` would mean fabricating a dozen fields
 * (bindings, visibility, timestamps) it does not have and the card never reads.
 */
export interface AgentLaunchCardView {
  /** Required: the tile is drawn from this and nothing else (see `AgentIconComponent`). */
  agentId: string;
  name: string;
  /** The shelf subtitle (D4). Falls back to the description's first line when absent. */
  tagline?: string;
  description?: string;
  ownerName?: string;
  publisher?: ListingPublisher | null;
  categoryLabel?: string;
  emoji?: string;
  iconUrl?: string;
  starters: string[];
  /**
   * Whether this agent is in the store, which gates the two store affordances — the
   * Add/Added pill and the link to the detail page. A private agent has no detail page
   * to link to and nothing to add; rendering either would be an affordance with no
   * destination behind it.
   */
  listed: boolean;
}

/**
 * Projects the loaded record into the card's view.
 *
 * `Agent` rather than `Assistant` on purpose: tagline, publisher and category exist only
 * on the Agent read shape, and the session page already fetches it (`loadAgentBindings`)
 * to lock the chat-input pickers. Reading the card from the same response keeps one
 * source instead of two half-populated ones.
 */
export function agentLaunchCardView(agent: Agent): AgentLaunchCardView {
  return {
    agentId: agent.agentId,
    name: agent.name,
    tagline: agent.tagline,
    description: agent.description,
    ownerName: agent.ownerName,
    publisher: agent.publisher,
    categoryLabel: agent.categoryLabel,
    emoji: agent.emoji,
    iconUrl: agent.iconUrl,
    starters: agent.starters ?? [],
    listed: agent.listing?.state === 'published',
  };
}

/**
 * The card that greets you when an Agent opens — the store's detail page, folded to chat
 * width.
 *
 * Deliberately the same reading order as the page the user tapped through to get here:
 * tile, name, tagline, who made it, what to ask. The shelf sells on the tagline, which
 * used to vanish at the exact moment the user was deciding what to type.
 *
 * What it deliberately does *not* carry is inventory: the capability chips and the green
 * "ready to run" line both restated, at the moment of typing, something the user had
 * already been told on the way in. Only the *blocked* half of D6 survives — "you can't
 * run this and here is what's missing" is news; "this works" is not.
 *
 * ⚠️ The tile is `app-agent-icon` and must stay that way. The card this replaced hashed the
 * **first letter of the name** into its own 26-gradient palette while every other surface
 * hashed `agentId`, so the same Agent was drawn two different ways either side of a tap —
 * exactly what the note on `hashAgentId` exists to prevent. Two of those gradients were
 * also near-white under white text.
 *
 * Starters are **buttons**, unlike the detail page's read-only list: the chat has real
 * composer prefill behind `starterSelected`, so here there is a lever to attach them to.
 *
 * The footer's blocked line is the one thing the store cannot tell you (D6) and the
 * header's pill is the one action it can — kept apart so state never sits where an action
 * is expected.
 */
@Component({
  selector: 'app-agent-launch-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, RouterLink, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowRight,
      heroCheck,
      heroCheckBadge,
      heroLockClosed,
      heroNoSymbol,
      heroPlus,
      heroXMark,
    }),
  ],
  template: `
    @if (view(); as v) {
      <div
        class="w-full max-w-lg overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800"
      >
        <div class="p-5">
          <!-- Identity -->
          <div class="flex items-start gap-3.5">
            <app-agent-icon
              [agentId]="v.agentId"
              [iconUrl]="v.iconUrl"
              [emoji]="v.emoji"
              [size]="52"
            />

            <div class="min-w-0 flex-1">
              <h2
                class="flex items-center gap-1.5 text-base/6 font-semibold text-gray-900 dark:text-white"
              >
                <span class="min-w-0 truncate">{{ v.name }}</span>
                @if (v.publisher?.verified) {
                  <ng-icon
                    name="heroCheckBadge"
                    class="size-4 shrink-0 text-state-info-600 dark:text-state-info-400"
                    [appTooltip]="verifiedTooltip()"
                    appTooltipPosition="top"
                  />
                }
              </h2>

              @if (subtitle()) {
                <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">{{ subtitle() }}</p>
              }

              @if (author() || v.categoryLabel) {
                <p
                  class="mt-1 flex flex-wrap items-center gap-x-1.5 text-sm/6 text-gray-500 dark:text-gray-400"
                >
                  @if (author(); as who) {
                    <span>By</span>
                    <span
                      [class]="
                        v.publisher?.verified
                          ? 'font-semibold text-state-info-600 dark:text-state-info-400'
                          : 'font-medium text-gray-700 dark:text-gray-300'
                     "
                      >{{ who }}</span
                    >
                  }
                  @if (v.categoryLabel) {
                    @if (author()) {
                      <span aria-hidden="true">·</span>
                    }
                    <span>{{ v.categoryLabel }}</span>
                  }
                </p>
              }
            </div>

            <div class="flex shrink-0 items-center gap-1">
              @if (v.listed) {
                @if (isLocked()) {
                  <!--
                    A locked role pin (D9.4): the control is absent, not disabled. A
                    disabled toggle invites a click and then refuses it; a lock says who
                    decided.
                  -->
                  <span
                    class="inline-flex items-center gap-1.5 rounded-full border border-gray-300 bg-gray-50 px-3 py-1.5 text-sm/6 font-semibold text-gray-600 dark:border-gray-600 dark:bg-gray-900/40 dark:text-gray-300"
                    [appTooltip]="lockTooltip()"
                    appTooltipPosition="top"
                  >
                    <ng-icon name="heroLockClosed" class="size-4" aria-hidden="true" />
                    Added
                  </span>
                } @else {
                  <button
                    type="button"
                    (click)="onTogglePin()"
                    [disabled]="pinBusy()"
                    [appTooltip]="pinTooltip()"
                    appTooltipPosition="top"
                    [attr.aria-pressed]="isPinned()"
                    class="inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm/6 font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-45"
                    [class]="
                      isPinned()
                        ? 'border-state-success-200 bg-state-success-50 text-state-success-700 hover:bg-state-success-100 dark:border-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-300'
                        : 'border-gray-300 bg-white text-gray-900 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:hover:bg-gray-700'
                   "
                  >
                    <ng-icon
                      [name]="isPinned() ? 'heroCheck' : 'heroPlus'"
                      class="size-4"
                      aria-hidden="true"
                    />
                    {{ isPinned() ? 'Added' : 'Add' }}
                  </button>
                }
              }

              @if (closable()) {
                <button
                  type="button"
                  (click)="closed.emit()"
                  class="grid size-8 place-items-center rounded-full text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                  [attr.aria-label]="closeLabel()"
                >
                  <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
                </button>
              }
            </div>
          </div>

          @if (v.starters.length) {
            <div class="mt-5">
              <h3
                class="mb-2 text-xs/5 font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400"
              >
                Try asking
              </h3>
              <ul class="flex flex-col gap-1.5">
                @for (starter of v.starters; track starter) {
                  <li>
                    <button
                      type="button"
                      (click)="starterSelected.emit(starter)"
                      class="group flex w-full items-center gap-2.5 rounded-2xl border border-gray-200 bg-white px-3.5 py-2.5 text-left text-sm/6 text-gray-700 hover:border-gray-300 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600 dark:hover:bg-gray-700"
                    >
                      <span class="min-w-0 flex-1">{{ starter }}</span>
                      <ng-icon
                        name="heroArrowRight"
                        class="size-4 shrink-0 text-gray-300 transition-transform group-hover:translate-x-0.5 group-hover:text-gray-500 dark:text-gray-600 dark:group-hover:text-gray-300"
                        aria-hidden="true"
                      />
                    </button>
                  </li>
                }
              </ul>
            </div>
          }
        </div>

        @if (blockedText() || v.listed) {
          <div
            class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-gray-200 bg-gray-50 px-5 py-2.5 dark:border-gray-700 dark:bg-gray-900/40"
          >
            @if (blockedText(); as message) {
              <p
                class="flex items-start gap-1.5 text-sm/6 font-medium text-state-danger-700 dark:text-state-danger-400"
              >
                <ng-icon name="heroNoSymbol" class="mt-1 size-4 shrink-0" aria-hidden="true" />
                <span>{{ message }}</span>
              </p>
            } @else {
              <span></span>
            }

            @if (v.listed) {
              <a
                [routerLink]="['/agents', v.agentId]"
                class="shrink-0 text-sm/6 font-semibold text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
              >
                Agent details
              </a>
            }
          </div>
        }

        @if (pinError()) {
          <p
            role="alert"
            class="border-t border-gray-200 px-5 py-2.5 text-sm/6 text-state-danger-700 dark:border-gray-700 dark:text-state-danger-400"
          >
            {{ pinError() }}
          </p>
        }
      </div>
    }
  `,
  styles: [':host { display: flex; justify-content: center; }'],
})
export class AgentLaunchCardComponent {
  private pinService = inject(AgentPinService);

  readonly view = input.required<AgentLaunchCardView>();

  /**
   * D6, when the caller has it. Advisory and deliberately optional: the card is fully
   * usable without it, so a surface that cannot answer "will this run for me?" — or one
   * where the question is meaningless, like the author's own preview — simply omits it.
   *
   * A `ready` verdict renders nothing: passing it is still worth doing, because the card
   * is where a *blocked* one has to surface.
   */
  readonly runnability = input<AgentRunnability | null>(null);

  /** Whether to offer detaching this agent from the conversation. */
  readonly closable = input(false);

  readonly starterSelected = output<string>();
  readonly closed = output<void>();

  readonly pinBusy = signal(false);
  /**
   * Local rather than the pin service's own signal: that one also carries list-load
   * failures, and "failed to load your pinned agents" under the Add button would read as
   * a complaint about an action the user did not take.
   */
  readonly pinError = signal<string | null>(null);

  constructor() {
    // The pin list is session-wide and loads once, so this costs nothing on a surface
    // the user reached via the store — and it is the only thing that keeps the pill from
    // saying "Add" for an agent they already added a moment ago on another screen.
    effect(() => {
      if (this.view().listed) void this.pinService.load();
    });
  }

  /**
   * The tagline is the subtitle. Without one, the description stands in — truncated by
   * the browser rather than by us, so a long description degrades to one clamped line
   * instead of a wall of text above the starters.
   */
  readonly subtitle = computed(() => {
    const v = this.view();
    return v.tagline?.trim() || v.description?.trim() || '';
  });

  /**
   * Who stands behind the agent: the publisher when it has one, the owner otherwise. The
   * department outranks the person on a published agent — that is the name the reader is
   * being asked to trust, and the individual who happens to hold the record is noise.
   */
  readonly author = computed(() => {
    const v = this.view();
    return v.publisher?.label?.trim() || v.ownerName?.trim() || '';
  });

  /**
   * The blocked half of D6 only. "Ready to run for you." was a green line saying nothing
   * happened; this one names a missing grant, which the user can act on.
   */
  readonly blockedText = computed(() => {
    const r = this.runnability();
    return r && r.state !== 'ready' ? runnabilityMessage(r) : '';
  });

  isPinned(): boolean {
    return this.pinService.isPinned(this.view().agentId);
  }

  isLocked(): boolean {
    return this.pinService.isLocked(this.view().agentId);
  }

  closeLabel(): string {
    return `Leave ${this.view().name}`;
  }

  lockTooltip(): string {
    return `${this.view().name} is pinned for your role and can't be removed`;
  }

  pinTooltip(): string {
    return this.isPinned()
      ? `Remove ${this.view().name} from your agents`
      : `Add ${this.view().name} to your agents — a pointer, not a copy`;
  }

  verifiedTooltip(): string {
    const label = this.view().publisher?.label ?? 'a university team';
    return `Published by ${label} — a verified university team`;
  }

  async onTogglePin(): Promise<void> {
    this.pinBusy.set(true);
    this.pinError.set(null);
    try {
      await this.pinService.toggle(this.view().agentId);
    } catch {
      // The service has already rolled the list back and holds the message.
      this.pinError.set(this.pinService.error());
    } finally {
      this.pinBusy.set(false);
    }
  }
}
