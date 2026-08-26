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

          <div class="mt-6 grid gap-6 lg:grid-cols-2">
            <!-- Left: what it is -->
            <div class="flex flex-col gap-6">
              <section>
                <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Summary</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">{{ s.description }}</p>
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
                     line breaks, and reflowing it would misrepresent what they wrote. -->
                <pre
                  class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-gray-50 p-3 text-xs/5 text-gray-800 dark:bg-gray-900 dark:text-gray-200"
                >{{ s.instructions }}</pre>
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

            <!-- Right: what it does -->
            <app-review-test-drive
              class="h-full"
              [agentId]="s.agentId"
              [name]="s.name"
              [reviewVersion]="s.reviewVersion"
            />
          </div>

          <!-- Decision. Pinned at the foot so the reviewer acts where they finished
               reading, rather than scrolling back to the header. -->
          <div
            class="sticky bottom-0 mt-8 flex flex-col gap-3 border-t border-gray-200 bg-white/95 py-4 backdrop-blur sm:flex-row sm:items-center sm:justify-between dark:border-gray-700 dark:bg-gray-900/95"
          >
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
  readonly busy = signal(false);

  private readonly agentId = signal('');

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
    this.error.set(null);
    try {
      await this.service.review(this.agentId(), request);
      await this.router.navigate(['/admin/marketplace/review']);
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Failed to record the decision.');
      this.busy.set(false);
    }
  }

  private detail(err: unknown): string | null {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : null;
  }
}
