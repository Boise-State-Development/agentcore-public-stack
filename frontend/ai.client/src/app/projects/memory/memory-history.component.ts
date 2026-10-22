import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { UserService } from '../../auth/user.service';
import { ToastService } from '../../services/toast/toast.service';
import { personLabel } from '../../shared/utils/person';
import { parseIso } from '../../utils/date';
import { MemoryEntry, MemoryFileVersion, MemoryRestoreResponse, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryTextComponent } from './memory-text.component';
import { changeCounts, compareItems, renderItems } from './memory-text';

const REASONS: Record<string, string> = {
  edit: 'Edited',
  save: 'Saved by the assistant',
  proposal: 'Approved proposal',
  restore: 'Restored',
  baseline: 'Before history was kept',
  maintenance: 'Tidied by maintenance',
};

/**
 * A memory file's saved versions (shared-projects §6, 2.8c): the list beside the selected
 * version compared, item by item, with the file as it is now.
 *
 * Restoring makes the chosen version current as a **new** version, so history only grows
 * and the restore itself can be undone the same way. Items keep their anchors: one that
 * comes back keeps the history the archive held for it, and one the old version lacks goes
 * to the archive. Restoring takes the same right as editing.
 */
@Component({
  selector: 'app-memory-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, MemoryTextComponent],
  host: { class: 'block' },
  template: `
    <h2 class="text-xl/8 font-semibold text-gray-900 dark:text-white">History of <span class="font-mono">{{ entry().slug }}</span></h2>
    <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
      Every save is a version. Pick one to compare it with the file now{{ canEdit() ? ', and restore it if you need it back' : '' }}.
    </p>

    @if (loading()) {
      <div class="mt-6 h-32 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
    } @else if (error()) {
      <p role="alert" class="mt-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    } @else {
      <div class="mt-6 grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
        <ol class="flex flex-col gap-1" aria-label="Versions">
          @for (v of versions(); track v.version) {
            <li>
              <button
                type="button"
                (click)="select(v.version)"
                [attr.aria-current]="selected() === v.version ? 'true' : null"
                class="flex w-full items-start gap-3 rounded-2xl px-3.5 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="selected() === v.version ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-50 dark:hover:bg-white/5'"
              >
                <span class="w-9 shrink-0 font-mono text-xs/6 text-gray-600 dark:text-gray-400">v{{ v.version }}</span>
                <span class="min-w-0 flex-1">
                  <span class="block text-sm/6 text-gray-900 dark:text-white">
                    {{ reason(v) }}@if (v.version === currentVersion()) {<span class="ml-2 rounded-full border border-gray-300 px-2 text-xs/5 font-medium text-gray-700 dark:border-gray-600 dark:text-gray-200">Current</span>}
                  </span>
                  <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ who(v) }} · {{ at(v.updatedAt) | date: 'MMM d, y, h:mm a' }}</span>
                </span>
              </button>
            </li>
          }
        </ol>

        <article class="min-w-0 rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800" [attr.aria-busy]="versionLoading()">
          @if (selected() === currentVersion()) {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">This is the current version. Pick an earlier one to compare.</p>
          } @else if (versionLoading()) {
            <div class="h-24 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700"></div>
          } @else if (versionText() !== null) {
            <h3 class="text-base/7 font-semibold text-gray-900 dark:text-white">Version {{ selected() }} compared with now</h3>
            <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">{{ summary() }}</p>
            <div class="mt-3 grid gap-5 md:grid-cols-2">
              <section aria-labelledby="history-now">
                <h4 id="history-now" class="mb-2 text-xs/5 font-semibold tracking-wide text-gray-600 uppercase dark:text-gray-400">Now</h4>
                <ul class="flex flex-col gap-1.5">
                  @for (line of diff().current; track $index) {
                    <li class="rounded-lg px-2.5 py-1.5 text-sm/6 break-words" [class]="line.kind === 'removed' ? 'bg-state-danger-50 text-state-danger-900 line-through dark:bg-state-danger-900/30 dark:text-state-danger-200' : 'text-gray-700 dark:text-gray-300'">
                      @if (line.kind === 'removed') {<span class="sr-only">Not in version {{ selected() }}: </span>}<app-memory-text [text]="line.text" [entries]="entries()" />
                    </li>
                  } @empty {
                    <li class="text-sm/6 text-gray-600 dark:text-gray-400">Empty.</li>
                  }
                </ul>
              </section>
              <section aria-labelledby="history-then">
                <h4 id="history-then" class="mb-2 text-xs/5 font-semibold tracking-wide text-gray-600 uppercase dark:text-gray-400">Version {{ selected() }}</h4>
                <ul class="flex flex-col gap-1.5">
                  @for (line of diff().proposed; track $index) {
                    <li class="rounded-lg px-2.5 py-1.5 text-sm/6 break-words" [class]="line.kind === 'added' ? 'bg-state-success-50 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-200' : 'text-gray-700 dark:text-gray-300'">
                      @if (line.kind === 'added') {<span class="sr-only">Not in the file now: </span>}<app-memory-text [text]="line.text" [entries]="entries()" />
                    </li>
                  } @empty {
                    <li class="text-sm/6 text-gray-600 dark:text-gray-400">Empty.</li>
                  }
                </ul>
              </section>
            </div>
            @if (canEdit()) {
              <div class="mt-5 flex flex-wrap items-center justify-end gap-3 border-t border-gray-100 pt-5 dark:border-gray-700">
                <p class="mr-auto text-xs/5 text-gray-600 dark:text-gray-400">Restoring saves version {{ selected() }} as a new version. Nothing in history is lost.</p>
                <button
                  type="button"
                  (click)="restore()"
                  [disabled]="restoring()"
                  class="rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {{ restoring() ? 'Restoring…' : 'Restore version ' + selected() }}
                </button>
              </div>
            }
          }
          @if (actionError()) {
            <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ actionError() }}</p>
          }
        </article>
      </div>
    }
  `,
})
export class MemoryHistoryComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);
  private user = inject(UserService);

  readonly projectId = input.required<string>();
  readonly scope = input.required<MemoryScope>();
  readonly spaceId = input.required<string>();
  readonly entry = input.required<MemoryEntry>();
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly canEdit = input(false);
  readonly restored = output<MemoryRestoreResponse>();

  protected readonly versions = signal<MemoryFileVersion[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly selected = signal<number | null>(null);
  protected readonly versionText = signal<string | null>(null);
  protected readonly versionLoading = signal(false);
  protected readonly restoring = signal(false);
  protected readonly actionError = signal<string | null>(null);
  private readonly currentText = signal('');

  protected readonly currentVersion = computed(() => this.versions()[0]?.version ?? null);
  protected readonly diff = computed(() => compareItems(this.currentText(), this.versionText() ?? ''));
  protected readonly summary = computed(() => {
    const { added, removed } = changeCounts(this.diff());
    if (!added && !removed) return 'Its items are the same as now; only the order, description or aliases may differ.';
    const parts = [added ? `brings back ${count(added)}` : '', removed ? `takes out ${count(removed)}` : ''].filter(Boolean);
    return `Restoring it ${parts.join(' and ')}.`;
  });

  private readonly key = computed(() => `${this.projectId()}|${this.scope()}|${this.spaceId()}|${this.entry().slug}|${this.entry().version}`);

  constructor() {
    effect(() => {
      this.key();
      untracked(() => void this.load());
    });
  }

  protected reason(v: MemoryFileVersion): string {
    return REASONS[v.reason] ?? v.reason;
  }

  protected who(v: MemoryFileVersion): string {
    if (!v.updatedBy) return 'Someone';
    if (v.updatedBy.toLowerCase() === (this.user.currentUser()?.email ?? '').toLowerCase()) return 'You';
    return personLabel(v.updatedByName, v.updatedBy);
  }

  protected at(iso: string): Date {
    return parseIso(iso);
  }

  private async load(): Promise<void> {
    const key = this.key();
    this.loading.set(true);
    this.error.set(null);
    this.versionText.set(null);
    try {
      const [history, file] = await Promise.all([
        firstValueFrom(this.api.memoryHistory(this.spaceId(), this.entry().slug)),
        firstValueFrom(this.api.memoryFile(this.projectId(), this.scope(), this.entry().slug)),
      ]);
      if (key !== this.key()) return;
      this.versions.set(history.versions);
      this.currentText.set(renderItems(file.items));
      const previous = history.versions[1];
      this.selected.set(history.versions[0]?.version ?? null);
      if (previous) void this.select(previous.version);
    } catch (err) {
      if (key === this.key()) this.error.set(projectErrorMessage(err, 'This file’s history could not be loaded.'));
    } finally {
      if (key === this.key()) this.loading.set(false);
    }
  }

  protected async select(version: number): Promise<void> {
    this.selected.set(version);
    this.actionError.set(null);
    this.versionText.set(null);
    if (version === this.currentVersion()) return;
    this.versionLoading.set(true);
    try {
      const v = await firstValueFrom(this.api.memoryVersion(this.spaceId(), this.entry().slug, version));
      if (this.selected() === version) this.versionText.set(v.content);
    } catch (err) {
      if (this.selected() === version) this.actionError.set(projectErrorMessage(err, 'That version could not be opened.'));
    } finally {
      if (this.selected() === version) this.versionLoading.set(false);
    }
  }

  protected async restore(): Promise<void> {
    const version = this.selected();
    if (version === null || this.restoring()) return;
    this.restoring.set(true);
    this.actionError.set(null);
    try {
      const result = await firstValueFrom(this.api.restoreMemoryVersion(this.projectId(), this.scope(), this.entry().slug, version));
      this.toast.success(`Restored version ${version}`, `“${result.slug}” is now version ${result.version}.`);
      this.restored.emit(result);
    } catch (err) {
      // A pinned item the old version lacks, or a link or alias that no longer fits: the API says which.
      this.actionError.set(projectErrorMessage(err, 'That version could not be restored.'));
    } finally {
      this.restoring.set(false);
    }
  }
}

function count(n: number): string {
  return `${n} ${n === 1 ? 'item' : 'items'}`;
}
