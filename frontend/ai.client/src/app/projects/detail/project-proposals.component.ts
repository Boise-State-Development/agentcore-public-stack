import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroChevronRight, heroLightBulb } from '@ng-icons/heroicons/outline';
import { ToastService } from '../../services/toast/toast.service';
import { parseIso } from '../../utils/date';
import { personLabel } from '../../shared/utils/person';
import { MemoryProposal, MemoryProposalDetail, Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** The reviewer's note on a decision (`MAX_PROPOSAL_NOTE_CHARS`). */
export const MAX_PROPOSAL_NOTE_CHARS = 280;

export interface DiffLine {
  kind: 'same' | 'added' | 'removed';
  text: string;
}

const ANCHOR = /\s*<!--\s*e:[0-9a-z]+\s*-->/gi;

function items(text: string | null | undefined): string[] {
  return (text ?? '')
    .split('\n')
    .map(line => line.replace(ANCHOR, '').trimEnd())
    .filter(line => line.trim().length > 0);
}

/**
 * What a proposal changes, line by line, with item anchors hidden. Lines are matched by
 * their text, so an edited item reads as one removed and one added: enough to review a
 * memory file of one-line facts without a diff library.
 */
export function diffProposal(current: string | null | undefined, proposed: string): DiffLine[] {
  const before = items(current);
  const after = items(proposed);
  const kept = new Set(before);
  const now = new Set(after);
  return [
    ...after.map(text => ({ kind: kept.has(text) ? 'same' : 'added', text }) as DiffLine),
    ...before.filter(text => !now.has(text)).map(text => ({ kind: 'removed', text }) as DiffLine),
  ];
}

/**
 * Changes members have proposed to the project's shared memory (shared-projects 2.5a),
 * above Recents on the project page.
 *
 * Editors and the owner see every pending proposal and approve (as written, or edited
 * first) or decline it, with an optional note the proposer is sent. Anyone else sees
 * only their own pending proposals and can withdraw them. Renders nothing when there is
 * nothing pending, so most visits see no change to the page. The Memory tab (2.8) will
 * give this a fuller home with history.
 */
@Component({
  selector: 'app-project-proposals',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon],
  providers: [provideIcons({ heroChevronDown, heroChevronRight, heroLightBulb })],
  template: `
    @if (error()) {
      <p role="alert" class="mb-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    }
    @if (proposals().length > 0) {
      <section aria-labelledby="proposals-heading" class="mb-10">
        <h2 id="proposals-heading" class="flex items-center gap-2 text-sm/6 font-medium text-gray-600 dark:text-gray-400">
          <ng-icon name="heroLightBulb" class="size-4" aria-hidden="true" />
          {{ canReview() ? 'Proposed memory changes' : 'Your proposed memory changes' }}
          <span class="rounded-full bg-gray-100 px-2 text-xs/5 font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-200">{{ proposals().length }}</span>
        </h2>
        <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
          {{ canReview()
            ? 'Nothing in project memory changes until you approve. The person who proposed it is told either way.'
            : 'An editor will review these. You’ll get a notification when they decide.' }}
        </p>

        <ul class="mt-3 divide-y divide-gray-200/80 rounded-2xl border border-gray-200 dark:divide-white/10 dark:border-gray-700">
          @for (p of proposals(); track p.proposalId) {
            <li>
              <button
                type="button"
                (click)="toggle(p)"
                [attr.aria-expanded]="openId() === p.proposalId"
                [attr.aria-controls]="'proposal-' + p.proposalId"
                class="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-white/5"
              >
                <ng-icon [name]="openId() === p.proposalId ? 'heroChevronDown' : 'heroChevronRight'" class="mt-1 size-4 shrink-0 text-gray-500" aria-hidden="true" />
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                    {{ p.baseVersion ? 'Change to' : 'New file' }} “{{ p.slug }}”
                  </span>
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                    {{ p.isMine ? 'You' : personLabel(p.proposedByName, p.proposedByEmail) }}{{ p.proposerKind === 'agent' ? ', through the assistant' : '' }} · {{ created(p) | date: 'MMM d, h:mm a' }}
                  </span>
                </span>
                @if (p.stale) {
                  <span class="shrink-0 rounded-full bg-state-warning-50 px-2 py-0.5 text-xs/5 font-medium text-state-warning-700 dark:bg-state-warning-900/30 dark:text-state-warning-300">File changed since</span>
                }
              </button>

              @if (openId() === p.proposalId) {
                <div [id]="'proposal-' + p.proposalId" class="space-y-4 px-4 pt-1 pb-4 sm:pl-11">
                  @if (detailLoading()) {
                    <div class="h-16 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700" aria-busy="true"></div>
                  } @else if (detail(); as d) {
                    @if (d.stale) {
                      <p role="status" class="rounded-xl bg-state-warning-50 px-3 py-2 text-xs/5 text-state-warning-700 dark:bg-state-warning-900/20 dark:text-state-warning-300">
                        Someone changed “{{ d.slug }}” after this was proposed. Compare it with the file below, then approve an edited version or decline it.
                      </p>
                    }
                    <ul aria-label="What this changes" class="space-y-1 rounded-xl bg-gray-50 p-3 font-mono text-xs/5 dark:bg-gray-900/40">
                      @for (line of diff(); track $index) {
                        <li
                          class="whitespace-pre-wrap break-words rounded px-1.5"
                          [class]="line.kind === 'added'
                            ? 'bg-state-success-50 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300'
                            : line.kind === 'removed'
                              ? 'bg-state-danger-50 text-state-danger-800 line-through dark:bg-state-danger-900/30 dark:text-state-danger-300'
                              : 'text-gray-700 dark:text-gray-300'"
                        >
                          <span class="sr-only">{{ line.kind === 'added' ? 'Added: ' : line.kind === 'removed' ? 'Removed: ' : 'Unchanged: ' }}</span>
                          <span aria-hidden="true">{{ line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : '  ' }}</span>{{ line.text }}
                        </li>
                      } @empty {
                        <li class="text-gray-600 dark:text-gray-400">No change from the current file.</li>
                      }
                    </ul>

                    @if (canReview()) {
                      @if (editing()) {
                        <div>
                          <label [for]="'proposal-text-' + p.proposalId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Edited version</label>
                          <textarea
                            [id]="'proposal-text-' + p.proposalId"
                            rows="6"
                            [value]="editedText()"
                            (input)="editedText.set($any($event.target).value)"
                            class="mt-1 block w-full rounded-xl border border-gray-300 bg-white px-3 py-2 font-mono text-xs/5 text-gray-900 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                          ></textarea>
                          <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">One fact per “- ” line. Keep each item’s anchor comment so its history follows it.</p>
                        </div>
                      }
                      <div>
                        <label [for]="'proposal-note-' + p.proposalId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Note to {{ p.isMine ? 'yourself' : personLabel(p.proposedByName, p.proposedByEmail) }} (optional)</label>
                        <input
                          [id]="'proposal-note-' + p.proposalId"
                          type="text"
                          [attr.maxlength]="maxNote"
                          [value]="note()"
                          (input)="note.set($any($event.target).value)"
                          class="mt-1 block w-full rounded-xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                        />
                      </div>
                      <div class="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          (click)="approve(p)"
                          [disabled]="busy() || (d.stale && !editing())"
                          class="rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {{ editing() ? 'Approve edited version' : 'Approve' }}
                        </button>
                        <button
                          type="button"
                          (click)="reject(p)"
                          [disabled]="busy()"
                          class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                        >
                          Decline
                        </button>
                        <button
                          type="button"
                          (click)="toggleEditing(p)"
                          [disabled]="busy()"
                          class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-primary-50 dark:hover:bg-white/10"
                        >
                          {{ editing() ? 'Cancel edits' : 'Edit before approving' }}
                        </button>
                      </div>
                    } @else if (p.isMine) {
                      <button
                        type="button"
                        (click)="withdraw(p)"
                        [disabled]="busy()"
                        class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        Withdraw
                      </button>
                    }
                    @if (actionError()) {
                      <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ actionError() }}</p>
                    }
                  }
                </div>
              }
            </li>
          }
        </ul>
      </section>
    }
  `,
})
export class ProjectProposalsComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly personLabel = personLabel;
  protected readonly maxNote = MAX_PROPOSAL_NOTE_CHARS;

  readonly project = input.required<Project>();

  protected readonly proposals = signal<MemoryProposal[]>([]);
  protected readonly error = signal<string | null>(null);
  protected readonly openId = signal<string | null>(null);
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

  protected readonly diff = computed(() => {
    const d = this.detail();
    if (!d) return [];
    return diffProposal(d.currentText, this.editing() ? this.editedText() : d.text);
  });

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.proposals.set([]);
        this.openId.set(null);
        void this.load(id);
      });
    });
  }

  protected created(p: MemoryProposal): Date {
    return parseIso(p.createdAt);
  }

  private async load(projectId: string): Promise<void> {
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.proposals(projectId, 'pending'));
      if (projectId !== this.projectId()) return;
      this.proposals.set(response.proposals);
    } catch (err) {
      if (projectId !== this.projectId()) return;
      // Memory may be off for this deployment, or the project made before it: no section, no noise.
      if ((err as { status?: number })?.status === 409 || (err as { status?: number })?.status === 404) return;
      this.error.set(projectErrorMessage(err, 'Couldn’t load proposed memory changes.'));
    }
  }

  protected async toggle(p: MemoryProposal): Promise<void> {
    if (this.openId() === p.proposalId) {
      this.openId.set(null);
      return;
    }
    this.openId.set(p.proposalId);
    this.detail.set(null);
    this.editing.set(false);
    this.note.set('');
    this.actionError.set(null);
    this.detailLoading.set(true);
    try {
      const detail = await firstValueFrom(this.api.proposal(this.projectId(), p.proposalId));
      if (this.openId() === p.proposalId) this.detail.set(detail);
    } catch (err) {
      if (this.openId() === p.proposalId) this.actionError.set(projectErrorMessage(err, 'Couldn’t open this proposal.'));
    } finally {
      this.detailLoading.set(false);
    }
  }

  protected toggleEditing(p: MemoryProposal): void {
    if (!this.editing()) this.editedText.set(this.detail()?.text ?? p.text);
    this.editing.update(v => !v);
  }

  protected approve(p: MemoryProposal): Promise<void> {
    const note = this.note().trim() || undefined;
    const body = this.editing() ? { text: this.editedText(), note } : { note };
    return this.decide(p, () => this.api.approveProposal(this.projectId(), p.proposalId, body), `Saved “${p.slug}” to project memory.`);
  }

  protected reject(p: MemoryProposal): Promise<void> {
    const note = this.note().trim() || undefined;
    return this.decide(p, () => this.api.rejectProposal(this.projectId(), p.proposalId, note), `Declined the change to “${p.slug}”.`);
  }

  protected withdraw(p: MemoryProposal): Promise<void> {
    return this.decide(p, () => this.api.withdrawProposal(this.projectId(), p.proposalId), `Withdrew your change to “${p.slug}”.`);
  }

  private async decide(p: MemoryProposal, call: () => ReturnType<ProjectApiService['withdrawProposal']>, done: string): Promise<void> {
    this.busy.set(true);
    this.actionError.set(null);
    try {
      await firstValueFrom(call());
      this.proposals.update(list => list.filter(x => x.proposalId !== p.proposalId));
      this.openId.set(null);
      this.toast.success(done);
    } catch (err) {
      // A 409 means someone decided it first, or the file moved on: show the API's sentence.
      this.actionError.set(projectErrorMessage(err, 'That didn’t go through. Try again.'));
    } finally {
      this.busy.set(false);
    }
  }
}
