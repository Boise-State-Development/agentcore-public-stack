import { Component, ChangeDetectionStrategy, OnInit, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroExclamationTriangle, heroEye } from '@ng-icons/heroicons/outline';
import { AgentListingService } from '../services/agent-listing.service';
import {
  AgentCategory,
  AgentListingBlock,
  SkillExposure,
} from '../models/store.model';

export interface SubmitListingDialogData {
  agentId: string;
  agentName: string;
  /** Present on a resubmission; its category preselects the picker. */
  listing?: AgentListingBlock;
}

/** The listing after submission, or `undefined` if cancelled. */
export type SubmitListingDialogResult = AgentListingBlock | undefined;

/**
 * Submit an Agent to the marketplace (D2), with the D7 disclosures.
 *
 * The dialog does not decide anything. It asks `GET /agents/{id}/listing/preflight`,
 * which runs the same two checks the transition enforces, and renders the answers:
 *
 * * **Skill exposure (D7.1)** — publishing an Agent effectively publishes the contents
 *   of every skill its author wrote and bound, because Skills v2 resolves a `skill`
 *   binding on `skill.owner_id == agent.owner_id`. The names are listed, not counted.
 * * **Memory spaces (D7.2)** — a `memory_space` binding blocks submission outright, so
 *   Submit is disabled and the backend's message (which names the space) explains why.
 *   The author learns this before filling in a category, not after clicking.
 */
@Component({
  selector: 'app-submit-listing-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark, heroExclamationTriangle, heroEye })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative flex max-h-[90vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="submit-listing-title"
        aria-describedby="submit-listing-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="submit-listing-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ isResubmission() ? 'Submit again for review' : 'Submit to the marketplace' }}
            </h2>
            <p id="submit-listing-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              An admin reviews <span class="font-medium">{{ data.agentName }}</span> before it
              appears in the store. You'll see their decision here.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-6 py-4">
          @if (loading()) {
            <div class="space-y-3" aria-live="polite">
              <div class="h-4 w-1/3 animate-pulse rounded bg-gray-100 dark:bg-gray-700"></div>
              <div class="h-9 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-700"></div>
              <span class="sr-only">Checking what publishing this agent would share…</span>
            </div>
          } @else if (blockReason(); as reason) {
            <!-- D7.2 — not a warning. Nothing below it would help. -->
            <div
              role="alert"
              class="flex gap-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-200"
            >
              <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-5 shrink-0" aria-hidden="true" />
              <p>{{ reason }}</p>
            </div>
          } @else {
            <div>
              <label for="listing-category" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Category
              </label>
              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                Which shelf it sits on. A reviewer may move it.
              </p>
              <select
                id="listing-category"
                [value]="category()"
                (change)="onCategoryChange($event)"
                class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
              >
                <option value="" disabled>Choose a category…</option>
                @for (option of categories(); track option.id) {
                  <option [value]="option.id">{{ option.label }}</option>
                }
              </select>
              @if (!categories().length) {
                <p class="mt-2 text-xs/5 text-amber-700 dark:text-amber-400">
                  No categories are open for new listings yet. An admin adds these under
                  Admin → Marketplace → Categories.
                </p>
              }
            </div>

            <div class="mt-5">
              <label for="listing-note" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Note to the reviewer <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
              </label>
              <textarea
                id="listing-note"
                rows="3"
                [value]="note()"
                (input)="onNoteInput($event)"
                [placeholder]="notePlaceholder()"
                class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
              ></textarea>
            </div>

            <!-- D7.1 — enumerate, don't count. -->
            @if (exposedSkills().length; as count) {
              <div class="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-900 dark:bg-amber-900/20">
                <div class="flex gap-3">
                  <ng-icon
                    name="heroEye"
                    class="mt-0.5 size-5 shrink-0 text-amber-700 dark:text-amber-400"
                    aria-hidden="true"
                  />
                  <div class="min-w-0">
                    <p class="text-sm/6 font-medium text-amber-900 dark:text-amber-200">
                      {{ count === 1 ? '1 skill you wrote becomes' : count + ' skills you wrote become' }}
                      readable by anyone who runs this agent
                    </p>
                    <ul class="mt-1.5 space-y-0.5">
                      @for (skill of exposedSkills(); track skill.ref) {
                        <li class="text-sm/6 text-amber-900 dark:text-amber-200">· {{ skill.label }}</li>
                      }
                    </ul>
                  </div>
                </div>
              </div>
            }

            <p class="mt-5 text-xs/5 text-gray-500 dark:text-gray-400">
              You'll be credited as the publisher. An admin may reattribute the listing to a
              department or the university at approval — that changes the name on the shelf and
              nothing about who can run it.
            </p>

            @if (error(); as message) {
              <p role="alert" class="mt-4 text-sm/6 text-rose-700 dark:text-rose-400">{{ message }}</p>
            }
          }
        </div>

        <div class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            {{ blockReason() ? 'Close' : 'Cancel' }}
          </button>
          @if (!blockReason()) {
            <button
              type="button"
              [disabled]="!canSubmit()"
              (click)="onSubmit()"
              class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              {{ submitting() ? 'Submitting…' : 'Submit for review' }}
            </button>
          }
        </div>
      </div>
    </div>
  `,
})
export class SubmitListingDialogComponent implements OnInit {
  private dialogRef = inject<DialogRef<SubmitListingDialogResult>>(DialogRef);
  private listings = inject(AgentListingService);
  readonly data = inject<SubmitListingDialogData>(DIALOG_DATA);

  readonly categories = signal<AgentCategory[]>([]);
  readonly exposedSkills = signal<SkillExposure[]>([]);
  readonly blockReason = signal<string | null>(null);
  readonly loading = signal(true);
  readonly submitting = signal(false);
  readonly error = signal<string | null>(null);

  readonly category = signal('');
  readonly note = signal('');

  readonly isResubmission = computed(() => !!this.data.listing);

  readonly canSubmit = computed(
    () => !!this.category() && !this.submitting() && !this.blockReason(),
  );

  /** A resubmission is answering a reviewer; a first submission is introducing itself. */
  readonly notePlaceholder = computed(() =>
    this.isResubmission()
      ? 'What you changed since the last review.'
      : 'Anything the reviewer should know — who it is for, what it draws on.',
  );

  async ngOnInit(): Promise<void> {
    // Preselect the category the listing already had, so a resubmission is one click.
    this.category.set(this.data.listing?.category ?? '');
    try {
      const [categories, preflight] = await Promise.all([
        this.listings.loadCategories(),
        this.listings.preflight(this.data.agentId),
      ]);
      this.categories.set(categories);
      this.exposedSkills.set(preflight.exposedSkills ?? []);
      this.blockReason.set(preflight.blockReason ?? null);
      // Only preselect a category that is still open for new listings.
      if (!categories.some((c) => c.id === this.category())) {
        this.category.set('');
      }
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Could not check this agent for submission.');
    } finally {
      this.loading.set(false);
    }
  }

  onCategoryChange(event: Event): void {
    this.category.set((event.target as HTMLSelectElement).value);
  }

  onNoteInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  async onSubmit(): Promise<void> {
    if (!this.canSubmit()) return;
    this.submitting.set(true);
    this.error.set(null);
    try {
      const response = await this.listings.submit(this.data.agentId, {
        category: this.category(),
        note: this.note().trim() || undefined,
      });
      this.dialogRef.close(response.listing);
    } catch (err) {
      // The backend re-runs both D7 checks on the write, so a binding added since the
      // preflight surfaces here rather than passing silently.
      this.error.set(this.detail(err) ?? 'Failed to submit this agent for review.');
      this.submitting.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }

  private detail(err: unknown): string | null {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : null;
  }
}
