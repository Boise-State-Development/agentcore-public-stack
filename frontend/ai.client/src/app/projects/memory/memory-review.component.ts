import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { firstValueFrom, Observable } from 'rxjs';
import { ToastService } from '../../services/toast/toast.service';
import { personLabel } from '../../shared/utils/person';
import { parseIso } from '../../utils/date';
import { MemoryEntry, MemoryProposal, MemoryProposalDetail, Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryTextComponent } from './memory-text.component';
import { MAX_PROPOSAL_NOTE_CHARS, changeCounts, compareItems } from './memory-text';

/** What the page needs to hear after a decision. */
export interface ReviewOutcome {
  proposal: MemoryProposal;
  /** Approved: the file changed, so the page re-reads the project's files. */
  applied: boolean;
}

/**
 * Proposed changes to the project's shared memory (shared-projects 2.5a-1), reviewed in the
 * Memory tab (2.8): the queue on the left, the selected proposal's current and proposed
 * file side by side on the right.
 *
 * A proposal is a whole file, so it is approved or declined as one: there is one operation
 * per proposal (per-operation review belongs to 2.6's compaction proposals). Editors and the
 * owner see every pending proposal and approve it as written, edit it first, or decline it,
 * with a note the proposer is sent. Anyone else sees only their own and can withdraw them.
 * A proposal whose file changed since it was written can only be approved after editing.
 */
@Component({
  selector: 'app-memory-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, MemoryTextComponent],
  host: { class: 'block' },
  template: `
    <h2 class="text-xl/8 font-semibold text-gray-900 dark:text-white">{{ canReview() ? 'Review queue' : 'Your proposals' }}</h2>
    <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
      {{ canReview()
        ? 'Nothing in project memory changes until you approve. The person who proposed it is told either way.'
        : 'An editor will review these. You’ll get a notification when they decide.' }}
    </p>

    @if (loading()) {
      <div class="mt-6 h-32 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
    } @else if (error()) {
      <p role="alert" class="mt-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    } @else if (!proposals().length) {
      <div class="mt-6 rounded-2xl border border-dashed border-gray-300 px-6 py-10 text-center dark:border-gray-700">
        <p class="text-sm/6 font-semibold text-gray-900 dark:text-white">{{ canReview() ? 'All caught up' : 'Nothing waiting' }}</p>
        <p class="mx-auto mt-1 max-w-sm text-sm/6 text-gray-600 dark:text-gray-400">
          {{ canReview()
            ? 'No proposals are waiting. New ones from members and their tasks show up here.'
            : 'You have no proposals waiting for review.' }}
        </p>
      </div>
    } @else {
      <div class="mt-6 grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
        <ul class="flex flex-col gap-1" aria-label="Proposals">
          @for (p of proposals(); track p.proposalId) {
            <li>
              <button
                type="button"
                (click)="select(p.proposalId)"
                [attr.aria-current]="selectedId() === p.proposalId ? 'true' : null"
                class="flex w-full flex-col gap-0.5 rounded-2xl px-3.5 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="selectedId() === p.proposalId ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-50 dark:hover:bg-white/5'"
              >
                <span class="flex items-center gap-2">
                  <span class="text-xs/5 font-semibold text-gray-700 dark:text-gray-200">{{ p.baseVersion ? 'Change' : 'New file' }}</span>
                  <span class="truncate font-mono text-[0.8125rem] text-gray-900 dark:text-white">{{ p.slug }}</span>
                </span>
                <span class="truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ proposer(p) }} · {{ created(p) | date: 'MMM d, h:mm a' }}</span>
                @if (p.stale) {
                  <span class="mt-1 self-start rounded-full bg-state-warning-50 px-2 py-0.5 text-xs/5 font-medium text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300">File changed since</span>
                }
              </button>
            </li>
          }
        </ul>

        <article class="min-w-0 rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800" [attr.aria-busy]="detailLoading()">
          @if (selected(); as p) {
            <h3 class="text-base/7 font-semibold text-gray-900 dark:text-white">
              {{ p.baseVersion ? 'Change to' : 'New file' }} <span class="font-mono">{{ p.slug }}</span>
            </h3>
            @if (p.description) {
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">{{ p.description }}</p>
            }
            <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
              {{ proposer(p) }}{{ p.proposerKind === 'agent' ? ', through the assistant' : p.proposerKind === 'schedule' ? ', from a scheduled run' : '' }} · {{ created(p) | date: 'MMM d, y, h:mm a' }}
            </p>

            @if (detailLoading()) {
              <div class="mt-5 h-24 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700"></div>
            } @else if (detail(); as d) {
              @if (d.stale) {
                <p role="status" class="mt-4 rounded-xl bg-state-warning-50 px-3 py-2 text-xs/5 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-300">
                  Someone changed “{{ d.slug }}” after this was proposed. Compare the two, then approve an edited version or decline it.
                </p>
              }
              <p class="mt-4 text-xs/5 text-gray-600 dark:text-gray-400">{{ summary() }}</p>
              <div class="mt-3 grid gap-5 md:grid-cols-2">
                <section [attr.aria-labelledby]="'current-' + p.proposalId">
                  <h4 [id]="'current-' + p.proposalId" class="mb-2 text-xs/5 font-semibold tracking-wide text-gray-600 uppercase dark:text-gray-400">Current</h4>
                  <ul class="flex flex-col gap-1.5">
                    @for (line of diff().current; track $index) {
                      <li class="rounded-lg px-2.5 py-1.5 text-sm/6 break-words" [class]="line.kind === 'removed' ? 'bg-state-danger-50 text-state-danger-900 line-through dark:bg-state-danger-900/30 dark:text-state-danger-200' : 'text-gray-700 dark:text-gray-300'">
                        @if (line.kind === 'removed') {<span class="sr-only">Removed: </span>}<app-memory-text [text]="line.text" [entries]="entries()" />
                      </li>
                    } @empty {
                      <li class="rounded-lg border border-dashed border-gray-300 px-2.5 py-3 text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">New file. Nothing here yet.</li>
                    }
                  </ul>
                </section>
                <section [attr.aria-labelledby]="'proposed-' + p.proposalId">
                  <h4 [id]="'proposed-' + p.proposalId" class="mb-2 text-xs/5 font-semibold tracking-wide text-gray-600 uppercase dark:text-gray-400">{{ editing() ? 'Your edited version' : 'Proposed' }}</h4>
                  <ul class="flex flex-col gap-1.5">
                    @for (line of diff().proposed; track $index) {
                      <li class="rounded-lg px-2.5 py-1.5 text-sm/6 break-words" [class]="line.kind === 'added' ? 'bg-state-success-50 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-200' : 'text-gray-700 dark:text-gray-300'">
                        @if (line.kind === 'added') {<span class="sr-only">Added: </span>}<app-memory-text [text]="line.text" [entries]="entries()" />
                      </li>
                    } @empty {
                      <li class="rounded-lg border border-dashed border-gray-300 px-2.5 py-3 text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">Empty.</li>
                    }
                  </ul>
                </section>
              </div>

              @if (canReview()) {
                @if (editing()) {
                  <div class="mt-5">
                    <label [for]="'proposal-text-' + p.proposalId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Edited version</label>
                    <textarea
                      [id]="'proposal-text-' + p.proposalId"
                      rows="6"
                      [value]="editedText()"
                      (input)="editedText.set($any($event.target).value)"
                      [attr.aria-describedby]="'proposal-text-help-' + p.proposalId"
                      class="mt-1 block w-full rounded-xl border border-gray-300 bg-white px-3 py-2 font-mono text-xs/5 text-gray-900 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                    ></textarea>
                    <p [id]="'proposal-text-help-' + p.proposalId" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">One fact per “- ” line. Keep each item’s anchor comment so its history follows it.</p>
                  </div>
                }
                <div class="mt-4">
                  <label [for]="'proposal-note-' + p.proposalId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Note to {{ p.isMine ? 'yourself' : personLabel(p.proposedByName, p.proposedByEmail) }} (optional)</label>
                  <input
                    [id]="'proposal-note-' + p.proposalId"
                    type="text"
                    [attr.maxlength]="maxNote"
                    [value]="note()"
                    (input)="note.set($any($event.target).value)"
                    class="mt-1 block w-full rounded-xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                  />
                </div>
                <div class="mt-5 flex flex-wrap justify-end gap-2 border-t border-gray-100 pt-5 dark:border-gray-700">
                  <button type="button" (click)="reject(p)" [disabled]="busy()" class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
                    Decline
                  </button>
                  <button type="button" (click)="toggleEditing(p)" [disabled]="busy()" class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
                    {{ editing() ? 'Cancel edits' : 'Edit before approving' }}
                  </button>
                  <button type="button" (click)="approve(p)" [disabled]="busy() || (d.stale && !editing())" class="rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50">
                    {{ editing() ? 'Approve edited version' : 'Approve' }}
                  </button>
                </div>
              } @else if (p.isMine) {
                <div class="mt-5 flex justify-end border-t border-gray-100 pt-5 dark:border-gray-700">
                  <button type="button" (click)="withdraw(p)" [disabled]="busy()" class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
                    Withdraw
                  </button>
                </div>
              }
            }
            @if (actionError()) {
              <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ actionError() }}</p>
            }
          }
        </article>
      </div>
    }
  `,
})
export class MemoryReviewComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly personLabel = personLabel;
  protected readonly maxNote = MAX_PROPOSAL_NOTE_CHARS;

  readonly project = input.required<Project>();
  /** The shared memory's files, for resolving links. */
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly decided = output<ReviewOutcome>();

  protected readonly proposals = signal<MemoryProposal[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly selectedId = signal<string | null>(null);
  protected readonly detail = signal<MemoryProposalDetail | null>(null);
  protected readonly detailLoading = signal(false);
  protected readonly editing = signal(false);
  protected readonly editedText = signal('');
  protected readonly note = signal('');
  protected readonly busy = signal(false);
  protected readonly actionError = signal<string | null>(null);

  /** Owner or editor of an active project. An archived project takes no decisions. */
  protected readonly canReview = computed(() => {
    const p = this.project();
    return p.status === 'active' && (p.role === 'owner' || p.role === 'editor');
  });

  protected readonly selected = computed(() => this.proposals().find(p => p.proposalId === this.selectedId()) ?? null);

  protected readonly diff = computed(() => {
    const d = this.detail();
    if (!d) return { current: [], proposed: [] };
    return compareItems(d.currentText, this.editing() ? this.editedText() : d.text);
  });

  protected readonly summary = computed(() => {
    const { added, removed } = changeCounts(this.diff());
    if (!added && !removed) return 'No change from the current file.';
    const parts = [added ? `adds ${count(added)}` : '', removed ? `removes ${count(removed)}` : ''].filter(Boolean);
    return `This ${parts.join(' and ')}.`;
  });

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => void this.load(id));
    });
  }

  protected created(p: MemoryProposal): Date {
    return parseIso(p.createdAt);
  }

  protected proposer(p: MemoryProposal): string {
    return p.isMine ? 'You' : personLabel(p.proposedByName, p.proposedByEmail);
  }

  private async load(projectId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.proposals(projectId, 'pending'));
      if (projectId !== this.projectId()) return;
      this.proposals.set(response.proposals);
      const first = response.proposals[0];
      if (first) void this.select(first.proposalId);
    } catch (err) {
      if (projectId !== this.projectId()) return;
      this.error.set(projectErrorMessage(err, 'Couldn’t load proposed memory changes.'));
    } finally {
      if (projectId === this.projectId()) this.loading.set(false);
    }
  }

  protected async select(proposalId: string): Promise<void> {
    if (this.selectedId() === proposalId && this.detail()) return;
    this.selectedId.set(proposalId);
    this.detail.set(null);
    this.editing.set(false);
    this.note.set('');
    this.actionError.set(null);
    this.detailLoading.set(true);
    try {
      const detail = await firstValueFrom(this.api.proposal(this.projectId(), proposalId));
      if (this.selectedId() === proposalId) this.detail.set(detail);
    } catch (err) {
      if (this.selectedId() === proposalId) this.actionError.set(projectErrorMessage(err, 'Couldn’t open this proposal.'));
    } finally {
      if (this.selectedId() === proposalId) this.detailLoading.set(false);
    }
  }

  protected toggleEditing(p: MemoryProposal): void {
    if (!this.editing()) this.editedText.set(this.detail()?.text ?? p.text);
    this.editing.update(v => !v);
  }

  protected approve(p: MemoryProposal): Promise<void> {
    const note = this.note().trim() || undefined;
    const body = this.editing() ? { text: this.editedText(), note } : { note };
    return this.decide(p, this.api.approveProposal(this.projectId(), p.proposalId, body), `Saved “${p.slug}” to project memory.`, true);
  }

  protected reject(p: MemoryProposal): Promise<void> {
    const note = this.note().trim() || undefined;
    return this.decide(p, this.api.rejectProposal(this.projectId(), p.proposalId, note), `Declined the change to “${p.slug}”.`, false);
  }

  protected withdraw(p: MemoryProposal): Promise<void> {
    return this.decide(p, this.api.withdrawProposal(this.projectId(), p.proposalId), `Withdrew your change to “${p.slug}”.`, false);
  }

  private async decide(p: MemoryProposal, call: Observable<MemoryProposal>, done: string, applied: boolean): Promise<void> {
    this.busy.set(true);
    this.actionError.set(null);
    try {
      const result = await firstValueFrom(call);
      const index = this.proposals().findIndex(x => x.proposalId === p.proposalId);
      this.proposals.update(list => list.filter(x => x.proposalId !== p.proposalId));
      this.detail.set(null);
      this.selectedId.set(null);
      this.toast.success(done);
      this.decided.emit({ proposal: result, applied });
      const next = this.proposals()[Math.min(index, this.proposals().length - 1)];
      if (next) void this.select(next.proposalId);
    } catch (err) {
      // A 409 means someone decided it first, or the file moved on: show the API's sentence.
      this.actionError.set(projectErrorMessage(err, 'That didn’t go through. Try again.'));
    } finally {
      this.busy.set(false);
    }
  }
}

function count(n: number): string {
  return `${n} ${n === 1 ? 'item' : 'items'}`;
}
