import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminListingRow, AgentVersionSummary } from '../models/marketplace.model';

export interface RollbackDialogData {
  listing: AdminListingRow;
}

/** The chosen version and reason, or undefined if cancelled. */
export type RollbackDialogResult = { version: number; reason: string } | undefined;

/**
 * Repoint a published listing at a different snapshot (§8).
 *
 * **Versions are loaded here rather than with the Listings table.** The table is a list of
 * listings; a version history is a per-agent question that only matters once someone has
 * decided to change one, and fetching every agent's history to render a page of rows would
 * be the same mistake the review diff avoids.
 *
 * The currently-published version is shown but not selectable — "publish what is already
 * live" is not a decision, and the backend refuses it anyway. Offering it would make the
 * picker's most obvious entry the one that errors.
 *
 * ⚠️ **The copy is direction-neutral on purpose.** A rollback lowers the published pointer
 * but deletes nothing, so the newer snapshots are still there and putting one back is this
 * same dialog — the picker lists whatever is not live, above or below. Wording it as "roll
 * back to an earlier version" described the majority case and mislabelled the sequel to
 * every rollback, which is the one an admin reaches for under time pressure.
 */
@Component({
  selector: 'app-rollback-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark })],
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

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rollback-title"
        aria-describedby="rollback-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="rollback-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Publish a different version
            </h2>
            <p id="rollback-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              The store serves the version you pick, and everyone who opens
              <span class="font-medium">{{ data.listing.name }}</span> runs it from the next
              turn. Nothing is deleted — the version live now stays available to switch back
              to, and {{ data.listing.ownerName }}'s draft is untouched.
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

        <div class="px-6 py-4">
          @if (loading()) {
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading versions…</p>
          } @else if (error()) {
            <p role="alert" class="text-sm/6 text-rose-700 dark:text-rose-400">{{ error() }}</p>
          } @else if (selectable().length === 0) {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">
              This agent has only ever had one approved version, so there is nothing else to
              switch to.
            </p>
          } @else {
            <label
              for="rollback-version"
              class="block text-sm/6 font-medium text-gray-900 dark:text-white"
            >
              Version to publish
            </label>
            <select
              id="rollback-version"
              [value]="version()"
              (change)="onVersionChange($event)"
              class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
            >
              <option value="" disabled>Choose a version…</option>
              @for (v of selectable(); track v.version) {
                <option [value]="v.version">
                  v{{ v.version }}{{ v.name ? ' — ' + v.name : '' }}
                </option>
              }
            </select>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              Currently serving v{{ data.listing.publishedVersion }}.
            </p>

            <label
              for="rollback-reason"
              class="mt-4 block text-sm/6 font-medium text-gray-900 dark:text-white"
            >
              Why are you changing the published version?
            </label>
            <textarea
              id="rollback-reason"
              rows="3"
              [value]="reason()"
              (input)="onReasonInput($event)"
              placeholder="Be specific — this is the whole message the author receives."
              class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
            ></textarea>
          }
        </div>

        <div
          class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            [disabled]="!canSubmit()"
            (click)="onSubmit()"
            class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            Publish this version
          </button>
        </div>
      </div>
    </div>
  `,
})
export class RollbackDialogComponent implements OnInit {
  private dialogRef = inject<DialogRef<RollbackDialogResult>>(DialogRef);
  private service = inject(AdminMarketplaceService);
  readonly data = inject<RollbackDialogData>(DIALOG_DATA);

  readonly versions = signal<AgentVersionSummary[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly version = signal<number | ''>('');
  readonly reason = signal('');

  /** Everything except the one already live — see the class docstring. */
  readonly selectable = computed(() => this.versions().filter((v) => !v.isPublished));
  readonly canSubmit = computed(() => this.version() !== '' && !!this.reason().trim());

  async ngOnInit(): Promise<void> {
    try {
      const response = await this.service.loadVersions(this.data.listing.agentId);
      this.versions.set(response.versions ?? []);
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(
        typeof detail === 'string' ? detail : 'Could not load this agent’s versions.',
      );
    } finally {
      this.loading.set(false);
    }
  }

  onVersionChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.version.set(value ? Number(value) : '');
  }

  onReasonInput(event: Event): void {
    this.reason.set((event.target as HTMLTextAreaElement).value);
  }

  onSubmit(): void {
    const version = this.version();
    const reason = this.reason().trim();
    if (version !== '' && reason) {
      this.dialogRef.close({ version, reason });
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
