import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { UserService } from '../../auth/user.service';
import { ToastService } from '../../services/toast/toast.service';
import { parseIso } from '../../utils/date';
import { ArchivedMemoryItem, MemoryEntry, MemoryRestoreResponse, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryTextComponent } from './memory-text.component';

/**
 * Items that left a file and can still come back (shared-projects 2.5a-2): one a save left
 * out, or every item of a deleted file. Rows expire on their own, after a year for project
 * memory and 30 days for "Just me".
 *
 * Restoring puts the item back at the end of its file, with its original anchor and
 * history, recreating the file if it was deleted. It's a save, so it takes the same right
 * as editing: an editor of project memory, or the member in their own.
 */
@Component({
  selector: 'app-memory-archive',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, MemoryTextComponent],
  host: { class: 'block' },
  template: `
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-xl/8 font-semibold text-gray-900 dark:text-white">Archive</h2>
        <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
          Items removed from a file, or from a file that was deleted. They stay restorable for {{ scope() === 'project' ? 'a year' : '30 days' }}.
        </p>
      </div>
      @if (slugs().length > 1) {
        <div>
          <label for="archive-file" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">File</label>
          <select
            id="archive-file"
            [value]="filter()"
            (change)="filter.set($any($event.target).value)"
            class="mt-1 block rounded-xl border border-gray-300 bg-white py-1.5 pr-8 pl-3 text-sm/6 text-gray-900 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All files</option>
            @for (slug of slugs(); track slug) {
              <option [value]="slug">{{ slug }}</option>
            }
          </select>
        </div>
      }
    </div>

    @if (loading()) {
      <div class="mt-6 h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
    } @else if (error()) {
      <p role="alert" class="mt-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    } @else if (shown().length) {
      <ul class="mt-6 divide-y divide-gray-200 rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800" aria-label="Archived items">
        @for (row of shown(); track row.archiveId) {
          <li class="flex flex-col gap-3 p-4 sm:flex-row sm:items-start">
            <div class="min-w-0 flex-1">
              <p class="text-sm/6 break-words text-gray-900 dark:text-gray-100"><app-memory-text [text]="row.text" [entries]="entries()" /></p>
              <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                <span class="font-mono">{{ row.slug }}</span> ·
                {{ removedBy(row) }} {{ who(row.archivedBy) }} · {{ at(row.archivedAt) | date: 'MMM d, y' }} ·
                restorable until {{ at(row.restorableUntil) | date: 'MMM d, y' }}
              </p>
            </div>
            @if (canRestore()) {
              <button
                type="button"
                (click)="restore(row)"
                [disabled]="busy() !== null"
                [attr.aria-label]="'Restore to ' + row.slug + ': ' + row.text"
                class="shrink-0 self-start rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                {{ busy() === row.archiveId ? 'Restoring…' : 'Restore' }}
              </button>
            }
          </li>
        }
      </ul>
    } @else {
      <div class="mt-6 rounded-2xl border border-dashed border-gray-300 px-6 py-10 text-center dark:border-gray-700">
        <p class="text-sm/6 font-semibold text-gray-900 dark:text-white">Nothing in the archive</p>
        <p class="mx-auto mt-1 max-w-sm text-sm/6 text-gray-600 dark:text-gray-400">When an item is removed from a file, it lands here and can be put back.</p>
      </div>
    }
  `,
})
export class MemoryArchiveComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);
  private user = inject(UserService);

  readonly projectId = input.required<string>();
  readonly scope = input.required<MemoryScope>();
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly canRestore = input(false);
  /** An item went back into its file; the page re-reads that scope's files. */
  readonly restored = output<MemoryRestoreResponse>();

  protected readonly items = signal<ArchivedMemoryItem[]>([]);
  private readonly people = signal<Record<string, string>>({});
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<string | null>(null);
  protected readonly filter = signal('');

  protected readonly slugs = computed(() => [...new Set(this.items().map(i => i.slug))].sort());
  protected readonly shown = computed(() => {
    const slug = this.filter();
    return slug ? this.items().filter(i => i.slug === slug) : this.items();
  });

  private readonly key = computed(() => `${this.projectId()}|${this.scope()}`);

  constructor() {
    effect(() => {
      this.key();
      untracked(() => {
        this.filter.set('');
        void this.load();
      });
    });
  }

  protected at(iso: string): Date {
    return parseIso(iso);
  }

  /** How the item left, ending where the person's name follows. */
  protected removedBy(row: ArchivedMemoryItem): string {
    switch (row.reason) {
      case 'deleted':
        return 'Its file was deleted by';
      case 'merged':
        return 'Merged into another item by maintenance, approved by';
      case 'superseded':
        return 'Replaced by a newer item by maintenance, approved by';
      case 'pruned':
        return 'Removed as past by maintenance, approved by';
      default:
        return 'Removed by';
    }
  }

  protected who(email: string): string {
    if (!email) return 'someone';
    if (email.toLowerCase() === (this.user.currentUser()?.email ?? '').toLowerCase()) return 'you';
    return this.people()[email] || email;
  }

  private async load(): Promise<void> {
    const key = this.key();
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.memoryArchive(this.projectId(), this.scope()));
      if (key !== this.key()) return;
      this.items.set(response.items);
      this.people.set(response.people ?? {});
    } catch (err) {
      if (key !== this.key()) return;
      this.items.set([]);
      this.error.set(projectErrorMessage(err, 'The archive could not be loaded.'));
    } finally {
      if (key === this.key()) this.loading.set(false);
    }
  }

  protected async restore(row: ArchivedMemoryItem): Promise<void> {
    this.busy.set(row.archiveId);
    try {
      const result = await firstValueFrom(this.api.restoreMemoryItem(this.projectId(), this.scope(), row.archiveId));
      this.items.update(list => list.filter(i => i.archiveId !== row.archiveId));
      if (!this.slugs().includes(this.filter())) this.filter.set('');
      this.toast.success(`Restored to “${result.slug}”`, `It’s back at the end of the file, as version ${result.version}.`);
      this.restored.emit(result);
    } catch (err) {
      this.toast.error('That item could not be restored', projectErrorMessage(err));
    } finally {
      this.busy.set(null);
    }
  }
}
