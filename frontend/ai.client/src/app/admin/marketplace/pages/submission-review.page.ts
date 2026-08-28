import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { ActivatedRoute, Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowUturnLeft,
  heroCheck,
  heroExclamationTriangle,
  heroEyeSlash,
  heroNoSymbol,
} from '@ng-icons/heroicons/outline';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminSubmissionReview, AgentCapability } from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import { ReviewDiffComponent } from '../components/review-diff.component';
import { ReviewTestDriveComponent } from '../components/review-test-drive.component';
import { reachabilityReviewerMessage } from '../../../agents/models/reachability';
import {
  RequestChangesDialogComponent,
  RequestChangesDialogData,
  RequestChangesDialogResult,
} from '../components/request-changes-dialog.component';
import {
  DeclineSubmissionDialogComponent,
  DeclineSubmissionDialogData,
  DeclineSubmissionDialogResult,
} from '../components/decline-submission-dialog.component';

/**
 * One submission, in full, with the decision at the bottom of it (D2).
 *
 * **What this page is for.** The review queue could name a submission but not show one.
 * `instructions` is gated to owner/editor on `GET /agents/{id}`, and that read 403s a
 * non-owner outright when the agent is PRIVATE — so the person deciding whether to publish
 * something could not read its system prompt, could not see what it binds, and on a *first*
 * submission had nothing at all to read, because the review diff is empty by construction
 * when nothing is published to diff against.
 *
 * ⚠️ **Everything here is the frozen snapshot.** The live record is the author's draft and
 * they can keep editing it while the row sits in the queue; approval promotes
 * `submittedVersion`. A page that read the draft would show one configuration and publish
 * another — which is the window `AgentVersion` was introduced to close. The one exception is
 * reachability, which is a fact about *now* and cannot come from a snapshot.
 *
 * Layout is deliberate: identity and warnings first, then instructions (the thing being
 * reviewed), then what it reaches, then the diff for a resubmission, with the test drive
 * beside it and the decision bar pinned at the foot. A reviewer should not have to scroll
 * back up to act on what they just read.
 */
@Component({
  selector: 'app-submission-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    NgIcon,
    AgentTileComponent,
    ReviewDiffComponent,
    ReviewTestDriveComponent,
  ],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowUturnLeft,
      heroCheck,
      heroExclamationTriangle,
      heroEyeSlash,
      heroNoSymbol,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <a
          routerLink="/admin/marketplace/review"
          class="inline-flex items-center gap-1.5 text-sm/6 font-medium text-blue-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Review queue
        </a>

        <!-- Load failures only. A *decision* failure renders down by the decision bar
             instead — see the note there. -->
        @if (error(); as message) {
          <div
            role="alert"
            class="mt-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ message }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-24">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading submission</span>
          </div>
        } @else if (submission(); as s) {
          <!-- Identity -->
          <div class="mt-4 flex flex-col gap-3 sm:flex-row sm:items-start">
            <app-agent-tile [agentId]="s.agentId" [iconUrl]="s.iconUrl" [emoji]="s.emoji" />
            <div class="min-w-0 flex-1">
              <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">{{ s.name }}</h1>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                {{ s.ownerName }} · {{ s.categoryLabel || s.category }}
                @if (s.publisher) {
                  · published as {{ s.publisher.label }}
                }
              </p>
              @if (s.tagline) {
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">{{ s.tagline }}</p>
              }
            </div>
          </div>

          <!-- Warnings, before anything a reviewer might act on. -->
          @if (reachabilityWarning(); as warning) {
            <p
              class="mt-4 flex items-start gap-1.5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-800 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-300"
            >
              <ng-icon name="heroEyeSlash" class="mt-1 size-4 shrink-0" aria-hidden="true" />
              <span>{{ warning }}</span>
            </p>
          }
          @if (s.snapshotUnavailable) {
            <!-- Not an error and not a blocker: the reviewer can still read and decide.
                 What they must not do is assume the text below is pinned, because the
                 author can change it under them. -->
            <p
              class="mt-3 flex items-start gap-1.5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-800 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-300"
            >
              <ng-icon
                name="heroExclamationTriangle"
                class="mt-1 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>
                This submission predates version snapshots, so what you see below is the
                agent's live record — its author can still change it. Approving it is
                refused; ask them to resubmit and a snapshot is captured on the way in.
              </span>
            </p>
          }

          <!-- items-start so the right column can be sticky: a stretched grid cell has
               no room to stick within. -->
          <div class="mt-6 grid items-start gap-6 lg:grid-cols-2">
            <!-- Left: what it is -->
            <!-- A whole-string class binding rather than a per-class one: a class-binding
                 *name* cannot contain a colon, so Tailwind's breakpoint prefixes have to be
                 toggled as a string. -->
            <div [class]="readColumnClasses()">
              <section>
                <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Summary</h2>
                @if (s.description.trim()) {
                  <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">{{ s.description }}</p>
                } @else {
                  <p class="mt-1 text-sm/6 italic text-gray-500 dark:text-gray-400">
                    None — this agent has no summary.
                  </p>
                }
              </section>

              <section>
                <div class="flex items-baseline justify-between gap-2">
                  <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                    Instructions
                  </h2>
                  @if (s.reviewVersion; as version) {
                    <span class="text-xs/5 text-gray-500 dark:text-gray-400">
                      version {{ version }}
                    </span>
                  }
                </div>
                <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                  The system prompt this agent runs with.
                </p>
                <!-- Monospace, pre-wrapped, scrollable: an author's prose has intentional
                     line breaks, and reflowing it would misrepresent what they wrote.

                     ⚠️ An empty system prompt gets a sentence, not an empty box. A blank
                     panel under a heading reads as a page that failed to load, and a
                     reviewer who thinks the read is broken cannot tell it apart from an
                     agent that genuinely ships no instructions — which is itself a reason
                     to decline, and so must be legible rather than ambiguous. -->
                @if (s.instructions.trim()) {
                  <pre
                    class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-gray-50 p-3 text-xs/5 text-gray-800 dark:bg-gray-900 dark:text-gray-200"
                  >{{ s.instructions }}</pre>
                } @else {
                  <p class="mt-2 text-sm/6 italic text-gray-500 dark:text-gray-400">
                    None — this agent ships no system prompt and will behave like a plain
                    chat.
                  </p>
                }
              </section>

              <section>
                <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                  What it reaches
                </h2>
                <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                  Tools, skills and memory spaces bound by this version.
                </p>
                @if (s.capabilities.length) {
                  <ul class="mt-2 flex flex-wrap gap-1.5">
                    @for (capability of s.capabilities; track capabilityKey(capability)) {
                      <li
                        class="rounded-2xl bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-200"
                      >
                        {{ capability.label }}
                        <span class="font-normal text-gray-500 dark:text-gray-400">
                          · {{ kindLabel(capability.kind) }}
                        </span>
                      </li>
                    }
                  </ul>
                } @else {
                  <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-300">
                    Nothing — this agent answers from its instructions alone.
                  </p>
                }
                <dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm/6">
                  <dt class="text-gray-500 dark:text-gray-400">Model</dt>
                  <dd class="text-gray-900 dark:text-white">{{ s.modelLabel || '—' }}</dd>
                  <dt class="text-gray-500 dark:text-gray-400">Starters</dt>
                  <dd class="text-gray-900 dark:text-white">
                    @if (s.starters.length) {
                      <ul>
                        @for (starter of s.starters; track $index) {
                          <li>{{ starter }}</li>
                        }
                      </ul>
                    } @else {
                      —
                    }
                  </dd>
                </dl>
              </section>

              @if (!isWithdrawal()) {
                <section>
                  <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                    Against what is published
                  </h2>
                  <app-review-diff [agentId]="s.agentId" />
                </section>
              }
            </div>

            <!-- Right: what it does.
                 Sized against the viewport rather than against the left column, which is
                 what made it unusable: a chat surface whose height is "whatever the prose
                 beside it happens to need" leaves ~120px between the banner and the
                 composer, and a reviewer cannot judge an agent through a two-line window.
                 Sticky so it stays put while they scroll the instructions — reading and
                 asking are the same loop, not two passes. -->
            <app-review-test-drive
              [class]="testDriveClasses()"
              [agentId]="s.agentId"
              [name]="s.name"
              [reviewVersion]="s.reviewVersion"
              [expanded]="expanded()"
              (expandedChange)="expanded.set($event)"
            />
          </div>

          <!-- Decision. Pinned at the foot so the reviewer acts where they finished
               reading, rather than scrolling back to the header. -->
          <div
            class="sticky bottom-0 mt-8 flex flex-col gap-3 border-t border-gray-200 bg-white/95 py-4 backdrop-blur dark:border-gray-700 dark:bg-gray-900/95"
          >
            <!-- ⚠️ A refused decision reports itself HERE, not at the top of the page.
                 The decision bar is sticky and the read above it is not, so on a submission
                 with real instructions the reviewer presses Approve at the bottom of a
                 scrolled page and a message rendered at the top is simply off-screen. That
                 is the gap the global toast used to paper over; removing the toast without
                 moving the message would have made the failure silent rather than
                 duplicated. -->
            @if (decisionError(); as message) {
              <p
                role="alert"
                class="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-2 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
              >
                {{ message }}
              </p>
            }

            <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">
              @if (isWithdrawal()) {
                The author asked to pull this listing. Decide it from the queue.
              } @else if (isDecidable()) {
                Approving publishes it to the store immediately.
              } @else {
                This listing has no decision waiting — you are reading it as a record.
              }
            </p>
            @if (isDecidable()) {
              <div class="flex shrink-0 flex-wrap gap-2">
                <button
                  type="button"
                  [disabled]="busy()"
                  (click)="decline()"
                  class="inline-flex items-center gap-1.5 rounded-2xl border border-rose-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-rose-700 hover:bg-rose-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-rose-800 dark:bg-gray-800 dark:text-rose-300 dark:hover:bg-rose-900/20"
                >
                  <ng-icon name="heroNoSymbol" class="size-4" aria-hidden="true" />
                  Decline
                </button>
                <button
                  type="button"
                  [disabled]="busy()"
                  (click)="requestChanges()"
                  class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroArrowUturnLeft" class="size-4" aria-hidden="true" />
                  Request changes
                </button>
                <button
                  type="button"
                  [disabled]="busy()"
                  (click)="approve()"
                  class="inline-flex items-center gap-1.5 rounded-2xl bg-blue-600 px-3 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
                  Approve
                </button>
              </div>
            }
            </div>
          </div>
        }
      </div>
    </div>
  `,
})
export class SubmissionReviewPage implements OnInit {
  private readonly service = inject(AdminMarketplaceService);
  private readonly dialog = inject(Dialog);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly submission = signal<AdminSubmissionReview | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  /**
   * A refused decision, kept apart from ``error`` so it can render beside the buttons.
   *
   * Two error regions rather than one because they are read at different moments and from
   * different scroll positions: a load failure is the first thing on an otherwise empty
   * page, while a decision failure answers a button in a sticky bar the reviewer may have
   * scrolled a long way to reach.
   */
  readonly decisionError = signal<string | null>(null);
  readonly busy = signal(false);

  private readonly agentId = signal('');

  /**
   * Whether the test drive has taken the full width of the page.
   *
   * Held here rather than inside the panel because it changes the *page's* grid: expanding
   * spans the panel across both columns and pushes the read above it. Toggled by class
   * binding only — never by moving the element in the DOM, which would destroy the
   * component and take the reviewer's conversation with it.
   */
  readonly expanded = signal(false);

  /**
   * Sticky and viewport-tall in the column; full-width and tall when expanded.
   *
   * `100dvh` rather than `100vh` so a mobile browser's collapsing toolbar does not leave
   * the composer under the chrome.
   */
  testDriveClasses(): string {
    return this.expanded()
      ? 'h-[calc(100dvh-14rem)] min-h-[28rem] lg:col-span-2'
      : 'h-[32rem] lg:sticky lg:top-6 lg:h-[calc(100dvh-10rem)] lg:min-h-[28rem]';
  }

  /** The read column takes the full width once the test drive is spanning both. */
  readColumnClasses(): string {
    return this.expanded()
      ? 'flex flex-col gap-6 lg:col-span-2'
      : 'flex flex-col gap-6';
  }

  /**
   * Only a pending *submission* gets the three decisions.
   *
   * A withdrawal request is a different question with opposite answers, and it is decided
   * in the queue — answering it with the submission verbs would re-publish over the
   * author's request without ever saying so, which is the failure the queue's own
   * `isWithdrawal` split exists to prevent.
   */
  readonly isDecidable = computed(() => this.submission()?.state === 'in_review');
  readonly isWithdrawal = computed(() => this.submission()?.state === 'withdrawal_requested');

  private readonly kindLabels: Record<string, string> = {
    tool: 'tool',
    skill: 'skill',
    memory_space: 'memory space',
    knowledge_base: 'knowledge base',
  };

  ngOnInit(): void {
    this.agentId.set(this.route.snapshot.paramMap.get('agentId') ?? '');
    void this.reload();
  }

  async reload(): Promise<void> {
    const id = this.agentId();
    if (!id) {
      this.loading.set(false);
      this.error.set('No agent was named in this link.');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    this.decisionError.set(null);
    try {
      this.submission.set(await this.service.loadSubmission(id));
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Could not load this submission.');
    } finally {
      this.loading.set(false);
    }
  }

  reachabilityWarning(): string | null {
    const s = this.submission();
    return s ? reachabilityReviewerMessage(s.reachability) : null;
  }

  capabilityKey(capability: AgentCapability): string {
    return `${capability.kind}:${capability.label}`;
  }

  kindLabel(kind: string): string {
    return this.kindLabels[kind] ?? kind;
  }

  async approve(): Promise<void> {
    await this.decide({ decision: 'approve' });
  }

  async requestChanges(): Promise<void> {
    const s = this.submission();
    if (!s) return;
    const ref = this.dialog.open<RequestChangesDialogResult, RequestChangesDialogData>(
      RequestChangesDialogComponent,
      { data: { listing: { name: s.name, ownerName: s.ownerName } } },
    );
    const note = await firstValueFrom(ref.closed);
    if (note) await this.decide({ decision: 'request_changes', note });
  }

  async decline(): Promise<void> {
    const s = this.submission();
    if (!s) return;
    const ref = this.dialog.open<DeclineSubmissionDialogResult, DeclineSubmissionDialogData>(
      DeclineSubmissionDialogComponent,
      { data: { name: s.name, ownerName: s.ownerName } },
    );
    const note = await firstValueFrom(ref.closed);
    if (note) await this.decide({ decision: 'reject', note });
  }

  /**
   * Record the decision and return to the queue.
   *
   * Navigating away rather than reloading in place: every decision moves the listing out
   * of `in_review`, so staying here would leave the reviewer on a page whose whole action
   * bar has just gone, looking at a submission that is no longer theirs to decide.
   */
  private async decide(request: {
    decision: 'approve' | 'request_changes' | 'reject';
    note?: string;
  }): Promise<void> {
    this.busy.set(true);
    this.decisionError.set(null);
    try {
      await this.service.review(this.agentId(), request);
      await this.router.navigate(['/admin/marketplace/review']);
    } catch (err) {
      this.decisionError.set(this.detail(err) ?? 'Failed to record the decision.');
      this.busy.set(false);
    }
  }

  private detail(err: unknown): string | null {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : null;
  }
}
