import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  OnInit,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowRight,
  heroCheck,
  heroCheckBadge,
  heroCheckCircle,
  heroExclamationTriangle,
  heroFlag,
  heroLockClosed,
  heroNoSymbol,
  heroPlus,
} from '@ng-icons/heroicons/outline';
import { AgentApiService } from '../services/agent-api.service';
import { AgentPinService } from '../services/agent-pin.service';
import { Agent, AgentRunnability } from '../models/agent.model';
import { runnabilityIcon, runnabilityMessage } from '../models/runnability';
import { AgentIconComponent } from '../components/agent-icon.component';
import {
  ReportAgentDialogComponent,
  ReportAgentDialogData,
  ReportAgentDialogResult,
} from '../components/report-agent-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { parseIso } from '../../utils/date';

/**
 * Agent detail — the page a shelf row taps through to (Marketplace Phase 3).
 *
 * The shelf deliberately carries icon, name and one line (D4), which means this page
 * is where everything else has to land: what the Agent is, what it can reach, and —
 * the one thing D4's shelf cannot tell you — whether it will run for *you* (D6).
 *
 * Two loads, deliberately not one: the identity half paints from `GET /agents/{id}`
 * immediately, while runnability fans out across the viewer's model/tool/skill catalogs
 * and settles into the sidebar when it arrives. Blocking the whole page on the slower
 * question would trade a fast page for a spinner.
 *
 * "Add to my agents" (D8, Phase 5) sits beside Start chat and is a **pin, never a fork**:
 * it stores a pointer, so the Agent the user opens tomorrow is the one its author
 * maintains. It is deliberately *not* gated on runnability — a user may reasonably keep
 * an Agent they cannot run today, and hiding the button would leave them nothing to do
 * with the page.
 *
 * "Report a problem" (D15, Phase 8) is at the foot of the page, not beside those two, and
 * that placement is the design: it is the exit, not an action the page is inviting. It
 * renders only for a **published** agent (D15.3) — you may report what the store offered
 * you; a private or in-review agent nobody outside the author was invited to has no
 * takedown available as a remedy either.
 *
 * ⚠️ Nothing about reports is ever shown *here* to anyone else. No count, no other user's
 * text, no badge. A report is a private message to the curator; the moment this page
 * rendered report volume, reporting would become a way to bury a competitor's agent.
 */
@Component({
  selector: 'app-agent-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon, TooltipDirective, SpinnerComponent],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowRight,
      heroCheck,
      heroCheckBadge,
      heroCheckCircle,
      heroExclamationTriangle,
      heroFlag,
      heroLockClosed,
      heroNoSymbol,
      heroPlus,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <button
          type="button"
          (click)="onBack()"
          class="mb-6 inline-flex items-center gap-1.5 text-sm/6 font-semibold text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Back
        </button>

        @if (error()) {
          <div
            role="alert"
            class="rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ error() }}
          </div>
        } @else if (loading()) {
          <div class="flex items-center justify-center py-16">
            <app-spinner size="lg" label="Loading agent" />
          </div>
        } @else if (agent(); as a) {
          <!-- Identity -->
          <div class="flex flex-wrap items-start gap-5">
            <app-agent-icon
              [agentId]="a.agentId"
              [iconUrl]="a.iconUrl"
              [emoji]="a.emoji"
              [size]="84"
            />
            <div class="min-w-0 flex-1">
              <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">{{ a.name }}</h1>
              @if (a.tagline) {
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">{{ a.tagline }}</p>
              }
              <p
                class="mt-2 flex flex-wrap items-center gap-2 text-sm/6 text-gray-500 dark:text-gray-400"
              >
                <span>{{ publisherLabel() }}</span>
                @if (a.publisher?.verified) {
                  <span class="inline-flex items-center gap-1 font-semibold text-state-info-600 dark:text-state-info-400">
                    <ng-icon
                      name="heroCheckBadge"
                      class="size-4"
                      [appTooltip]="verifiedTooltip()"
                      appTooltipPosition="top"
                    />
                    University team
                  </span>
                }
                @if (a.categoryLabel) {
                  <span aria-hidden="true">·</span>
                  <span>{{ a.categoryLabel }}</span>
                }
              </p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              @if (isLocked()) {
                <!--
                  A locked role pin (D9.4): it stays on this user's shelf, so the control
                  says who decided rather than offering an action that would be refused.
                -->
                <span
                  class="inline-flex items-center gap-1.5 rounded-full border border-gray-300 bg-gray-50 px-4 py-2 text-sm/6 font-semibold text-gray-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
                  [appTooltip]="lockTooltip()"
                  appTooltipPosition="top"
                >
                  <ng-icon name="heroLockClosed" class="size-4" aria-hidden="true" />
                  Added by your role
                </span>
              } @else {
              <button
                type="button"
                (click)="onTogglePin()"
                [disabled]="pinBusy()"
                [appTooltip]="pinTooltip()"
                appTooltipPosition="top"
                [attr.aria-pressed]="isPinned()"
                class="inline-flex items-center gap-1.5 rounded-full border px-4 py-2 text-sm/6 font-semibold disabled:cursor-not-allowed disabled:opacity-45"
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
              <button
                type="button"
                (click)="onStartChat()"
                [disabled]="isBlocked()"
                [appTooltip]="startChatTooltip()"
                appTooltipPosition="top"
                class="rounded-full bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-45"
              >
                Start chat
              </button>
            </div>
          </div>

          @if (pinError()) {
            <p
              role="alert"
              class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-400"
            >
              {{ pinError() }}
            </p>
          }

          <!--
            Hero: the @-mention prompt this agent answers to. Display-only — the
            composer has no prompt-prefill entry point, and @-mention itself is
            Phase 7, so a clickable pill here would be an affordance with no lever
            behind it. It shows the shape of the thing; Start chat performs it.
          -->
          <div
            class="mt-6 grid place-items-center rounded-3xl bg-linear-to-br from-blue-700 to-sky-500 px-6 py-12 dark:from-blue-900 dark:to-sky-700"
          >
            <p
              class="flex w-full max-w-xl items-center gap-4 rounded-full bg-white/95 px-5 py-3.5 text-sm/6 text-gray-900 shadow-md"
            >
              <span class="min-w-0 flex-1">
                <span class="font-semibold text-blue-700">{{ mention() }}</span>
                {{ heroSuffix() }}
              </span>
              <span
                class="grid size-7 shrink-0 place-items-center rounded-full bg-gray-900 text-white"
                aria-hidden="true"
              >
                <ng-icon name="heroArrowRight" class="size-3.5" />
              </span>
            </p>
          </div>

          <div class="mt-8 grid gap-8 lg:grid-cols-[1fr_300px]">
            <div>
              <section class="mb-8">
                <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">About</h2>
                <p class="max-w-prose text-sm/6 text-gray-600 dark:text-gray-400">
                  {{ a.description }}
                </p>
              </section>

              @if (a.starters.length) {
                <section class="mb-8">
                  <h2 class="mb-3 text-base/7 font-semibold text-gray-900 dark:text-white">
                    Try asking
                  </h2>
                  <!-- The author's starters, shown as examples. Not buttons: sending one
                       needs composer prefill, which does not exist yet. -->
                  <ul class="flex flex-col gap-2">
                    @for (starter of a.starters; track starter) {
                      <li
                        class="rounded-2xl border border-gray-200 bg-white px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
                      >
                        {{ starter }}
                      </li>
                    }
                  </ul>
                </section>
              }
            </div>

            <div class="flex flex-col gap-3">
              <section
                class="rounded-2xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800"
              >
                <h2
                  class="mb-2.5 text-xs/5 font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400"
                >
                  Details
                </h2>
                <dl class="flex flex-col gap-1">
                  @for (row of details(); track row.label) {
                    <div class="flex items-baseline justify-between gap-3 py-1 text-sm/6">
                      <dt class="text-gray-500 dark:text-gray-400">{{ row.label }}</dt>
                      <dd class="text-right font-semibold text-gray-900 dark:text-white">
                        {{ row.value }}
                      </dd>
                    </div>
                  }
                </dl>
              </section>

              <section
                class="rounded-2xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800"
              >
                <h2
                  class="mb-2.5 text-xs/5 font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400"
                >
                  What it can access
                </h2>
                @if (a.capabilities?.length) {
                  <ul class="flex flex-col gap-1">
                    @for (capability of a.capabilities; track capability.label) {
                      <li class="flex items-center gap-2 py-1 text-sm/6 text-gray-700 dark:text-gray-300">
                        <span class="min-w-0 flex-1 truncate">{{ capability.label }}</span>
                        <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">
                          {{ kindLabel(capability.kind) }}
                        </span>
                      </li>
                    }
                  </ul>
                } @else {
                  <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                    Nothing beyond its own knowledge.
                  </p>
                }

                <!-- D6: the one line the shelf deliberately does not carry. -->
                @if (runnability(); as r) {
                  <p
                    class="mt-3 flex items-start gap-1.5 border-t border-gray-200 pt-3 text-sm/6 dark:border-gray-700"
                    [class]="
                      r.state === 'ready'
                        ? 'text-state-success-700 dark:text-state-success-400'
                        : 'text-state-danger-700 dark:text-state-danger-400'
                   "
                  >
                    <ng-icon [name]="availabilityIcon()" class="mt-1 size-4 shrink-0" aria-hidden="true" />
                    <span>{{ availabilityText() }}</span>
                  </p>
                }
              </section>
            </div>
          </div>

          <!--
            D15. At the foot of the page and styled as a quiet link, because it is the
            exit rather than something the page is inviting. Published only (D15.3).
          -->
          @if (canReport()) {
            <div class="mt-10 border-t border-gray-200 pt-5 dark:border-gray-700">
              @if (reportConfirmation()) {
                <p
                  role="status"
                  class="flex items-start gap-2 text-sm/6 text-state-success-700 dark:text-state-success-400"
                >
                  <ng-icon name="heroCheckCircle" class="mt-1 size-4 shrink-0" aria-hidden="true" />
                  <span>{{ reportConfirmation() }}</span>
                </p>
              } @else {
                <button
                  type="button"
                  (click)="onReport()"
                  [disabled]="reportBusy()"
                  [appTooltip]="reportTooltip()"
                  appTooltipPosition="top"
                  class="inline-flex items-center gap-1.5 text-sm/6 font-medium text-gray-500 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:text-white"
                >
                  <ng-icon name="heroFlag" class="size-4" aria-hidden="true" />
                  Report a problem
                </button>
              }
              @if (reportError()) {
                <p role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
                  {{ reportError() }}
                </p>
              }
            </div>
          }
        }
      </div>
    </div>
  `,
})
export class AgentDetailPage implements OnInit {
  private api = inject(AgentApiService);
  private pinService = inject(AgentPinService);
  private router = inject(Router);
  private dialog = inject(Dialog);

  /** Bound from the `agents/:id` route via `withComponentInputBinding`. */
  readonly id = input.required<string>();

  readonly agent = signal<Agent | null>(null);
  readonly runnability = signal<AgentRunnability | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly pinBusy = signal(false);
  /**
   * Local rather than the service's signal: that one also carries list-load failures,
   * and "failed to load your pinned agents" under the Add button would read as a
   * complaint about an action the user did not take.
   */
  readonly pinError = signal<string | null>(null);

  readonly reportBusy = signal(false);
  readonly reportError = signal<string | null>(null);
  /**
   * The acknowledgement, which replaces the control rather than sitting beside it.
   *
   * Session-local on purpose: there is no read endpoint for "have I reported this", and
   * there should not be — that would hand every user a way to probe the queue. So the
   * page can only remember what it did itself, and the *server* is what actually enforces
   * one open report per reporter (D15.4).
   */
  readonly reportConfirmation = signal<string | null>(null);
  private readonly hasOpenReport = signal(false);

  /**
   * ``ngOnInit``, not the constructor: ``id`` is a required signal input bound by
   * ``withComponentInputBinding()``, and the router sets it *after* construction.
   * Reading it a moment too early throws NG0950, which ``load()``'s own try/catch
   * then swallowed into "Failed to load this agent." — an error banner with no
   * network request behind it, which is what this page did from phase 3 until now.
   */
  ngOnInit(): void {
    void this.load();
    // Cached across the session, so arriving from a shelf row costs nothing.
    void this.pinService.load();
  }

  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.agent.set(await firstValueFrom(this.api.getAgent(this.id())));
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load this agent.'));
      return;
    } finally {
      this.loading.set(false);
    }

    // Runnability is advisory: the page is fully usable without it, so a failure here
    // leaves the availability line absent rather than erroring the whole detail view.
    try {
      this.runnability.set(await firstValueFrom(this.api.getRunnability(this.id())));
    } catch {
      this.runnability.set(null);
    }
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }

  readonly isBlocked = computed(() => this.runnability()?.state === 'blocked');

  readonly publisherLabel = computed(
    () => this.agent()?.publisher?.label || this.agent()?.ownerName || 'Unknown',
  );

  readonly mention = computed(() => `@${this.agent()?.name ?? ''}`);

  /**
   * The hero band carries the `@Agent` prompt an author's first starter suggests,
   * lower-cased so it reads as one sentence after the mention.
   */
  readonly heroSuffix = computed(() => {
    const starter = this.agent()?.starters?.[0];
    if (!starter) return 'help me get started';
    return starter.charAt(0).toLowerCase() + starter.slice(1);
  });

  readonly details = computed(() => {
    const a = this.agent();
    if (!a) return [];
    return [
      { label: 'Publisher', value: this.publisherLabel() },
      { label: 'Category', value: a.categoryLabel || '—' },
      { label: 'Model', value: a.modelLabel || '—' },
      { label: 'Last updated', value: this.formatDate(a.updatedAt) },
    ];
  });

  /**
   * D6's two lines live in `models/runnability` because the chat launch card renders the
   * same answer — two copies would eventually be two different sentences for one state.
   */
  readonly availabilityIcon = computed(() => {
    const r = this.runnability();
    return r ? runnabilityIcon(r) : 'heroNoSymbol';
  });

  readonly availabilityText = computed(() => {
    const r = this.runnability();
    return r ? runnabilityMessage(r) : '';
  });

  isPinned(): boolean {
    return this.pinService.isPinned(this.id());
  }

  /** Pinned for the viewer's role and locked (D9.4) — there is no un-pinned state to offer. */
  isLocked(): boolean {
    return this.pinService.isLocked(this.id());
  }

  lockTooltip(): string {
    const name = this.agent()?.name ?? 'This agent';
    return `${name} is pinned for your role and can't be removed`;
  }

  pinTooltip(): string {
    const name = this.agent()?.name ?? 'this agent';
    return this.isPinned()
      ? `Remove ${name} from your agents`
      : `Add ${name} to your agents — a pointer, not a copy`;
  }

  async onTogglePin(): Promise<void> {
    this.pinBusy.set(true);
    this.pinError.set(null);
    try {
      await this.pinService.toggle(this.id());
    } catch {
      // The service has already rolled the list back and holds the message.
      this.pinError.set(this.pinService.error());
    } finally {
      this.pinBusy.set(false);
    }
  }

  /**
   * D15.3 — reportable means published.
   *
   * Read off the listing block the detail response already carries, so there is no second
   * request and no second copy of the rule; the server refuses an unpublished agent
   * regardless, and this only decides whether to render a control that would be refused.
   */
  readonly canReport = computed(() => this.agent()?.listing?.state === 'published');

  reportTooltip(): string {
    return `Tell the store's curators about a problem with ${this.agent()?.name ?? 'this agent'} — privately`;
  }

  async onReport(): Promise<void> {
    const agent = this.agent();
    if (!agent) return;

    const ref = this.dialog.open<ReportAgentDialogResult, ReportAgentDialogData>(
      ReportAgentDialogComponent,
      { data: { agentName: agent.name, hasOpenReport: this.hasOpenReport() } },
    );
    const result = await firstValueFrom(ref.closed);
    if (!result) return;

    this.reportBusy.set(true);
    this.reportError.set(null);
    try {
      const response = await firstValueFrom(this.api.reportAgent(this.id(), result));
      this.hasOpenReport.set(true);
      // The wording follows `replacedExisting` (D15.4) rather than assuming: telling
      // someone their report was "sent" when it amended an earlier one would leave them
      // expecting two things in the queue.
      this.reportConfirmation.set(
        response.replacedExisting
          ? 'Thanks — we updated the report you already had open on this agent.'
          : "Thanks — this went privately to the store's curators.",
      );
    } catch (err) {
      this.reportError.set(this.messageFor(err, 'Failed to send this report.'));
    } finally {
      this.reportBusy.set(false);
    }
  }

  startChatTooltip(): string {
    return this.isBlocked()
      ? 'This agent needs access your account does not have'
      : `Start a chat with ${this.agent()?.name ?? 'this agent'}`;
  }

  verifiedTooltip(): string {
    const label = this.agent()?.publisher?.label ?? 'a university team';
    return `Published by ${label} — a verified university team`;
  }

  kindLabel(kind: string): string {
    switch (kind) {
      case 'tool':
        return 'tool';
      case 'skill':
        return 'skill';
      case 'memory_space':
        return 'memory';
      default:
        return kind;
    }
  }

  private formatDate(iso: string): string {
    const parsed = parseIso(iso);
    if (Number.isNaN(parsed.getTime())) return '—';
    return parsed.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  }

  onBack(): void {
    void this.router.navigate(['/agents/discover']);
  }

  /**
   * Reuses the existing chat entry point — `assistantId` on the root route — because
   * `agentId === assistantId` and a store-launched run is an ordinary run (the invoker
   * pays, per the existing per-user model).
   */
  onStartChat(): void {
    if (this.isBlocked()) return;
    void this.router.navigate(['/'], { queryParams: { assistantId: this.id() } });
  }
}
