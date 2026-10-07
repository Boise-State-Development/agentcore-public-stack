import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroBookmark, heroPencilSquare, heroTrash } from '@ng-icons/heroicons/outline';
import { heroBookmarkSolid } from '@ng-icons/heroicons/solid';
import { UserService } from '../../auth/user.service';
import { ToastService } from '../../services/toast/toast.service';
import { personLabel } from '../../shared/utils/person';
import { parseIso } from '../../utils/date';
import { MemoryEntry, MemoryFile, MemoryItem, MemoryLimits, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryMeterComponent } from './memory-meter.component';
import { MemoryTextComponent } from './memory-text.component';
import { contributors, describeProvenance } from './memory-text';

/**
 * One memory file as items (shared-projects §6, 2.8): each item with where it came from
 * written underneath it, its pin, and its `[[links]]` as chips.
 *
 * Provenance is inline rather than on hover, because touch screens have no hover and
 * knowing where a fact came from is what makes shared memory trustworthy. An item from
 * the caller's own task links back to that task; anyone else's task stays private.
 *
 * Pinning is for whoever can edit the scope: a pinned item can't be dropped by any save,
 * the assistant's included, until a person unpins it.
 */
@Component({
  selector: 'app-memory-file-view',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, RouterLink, MemoryMeterComponent, MemoryTextComponent],
  providers: [provideIcons({ heroBookmark, heroBookmarkSolid, heroPencilSquare, heroTrash })],
  host: { class: 'block' },
  template: `
    <header class="border-b border-gray-200 p-5 dark:border-gray-700">
      <div class="flex flex-wrap items-start justify-between gap-3">
      <h2 class="min-w-0 font-mono text-lg/7 font-semibold break-all text-gray-900 dark:text-white">{{ entry().slug }}</h2>
      @if (canEdit() || canPropose()) {
        <div class="flex shrink-0 gap-2">
          <button
            type="button"
            (click)="canEdit() ? edit.emit() : propose.emit()"
            class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-200 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
            {{ canEdit() ? 'Edit' : 'Propose a change' }}
          </button>
          @if (canEdit()) {
            <button
              type="button"
              (click)="remove.emit()"
              [attr.aria-label]="'Delete ' + entry().slug"
              title="Delete this file"
              class="grid size-9 place-items-center rounded-2xl text-gray-500 transition-colors hover:bg-state-danger-50 hover:text-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-300"
            >
              <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
            </button>
          }
        </div>
      }
      </div>
      @if (entry().description) {
        <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">{{ entry().description }}</p>
      }
      @if (entry().aliases.length) {
        <p class="mt-2 flex flex-wrap items-center gap-1.5">
          <span class="sr-only">Also known as:</span>
          @for (alias of entry().aliases; track alias) {
            <span class="inline-flex items-center rounded-full border border-gray-300 px-2.5 py-0.5 text-xs/5 font-medium text-gray-700 dark:border-gray-600 dark:text-gray-200">also “{{ alias }}”</span>
          }
        </p>
      }
      <div class="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs/5 text-gray-600 dark:text-gray-400">
        <span>Version {{ entry().version || 1 }} · updated {{ updated() | date: 'MMM d, y' }}{{ entry().updatedBy ? ' by ' + updatedBy() : '' }}</span>
        @if (people().length) {
          <span [title]="peopleTitle()">{{ people().length }} {{ people().length === 1 ? 'contributor' : 'contributors' }}</span>
        }
        <app-memory-meter [tokens]="entry().tokens" [max]="limits().fileHardCapTokens" [soft]="limits().fileSoftThresholdTokens" label="File size" />
      </div>
      @if (nearCap()) {
        <p class="mt-3 rounded-xl bg-state-warning-50 px-3 py-2 text-xs/5 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-300">
          This file is close to the {{ limits().fileHardCapTokens.toLocaleString('en-US') }}-token limit for one file. A save that takes it over is refused, so split it or remove items that are no longer needed.
        </p>
      }
    </header>

    @if (loading()) {
      <div class="space-y-3 p-5" aria-busy="true">
        <div class="h-4 w-3/4 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
        <div class="h-4 w-1/2 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
      </div>
    } @else if (error()) {
      <p role="alert" class="p-5 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    } @else if (file(); as f) {
      @if (f.items.length) {
        <ul class="divide-y divide-gray-100 dark:divide-gray-700/60" aria-label="Items">
          @for (item of f.items; track item.anchor) {
            <li class="flex gap-3 px-5 py-3.5">
              <span class="mt-2.5 size-1.5 shrink-0 rounded-full bg-gray-500 dark:bg-gray-400" aria-hidden="true"></span>
              <div class="min-w-0 flex-1">
                <p class="text-sm/6 break-words text-gray-900 dark:text-gray-100"><app-memory-text [text]="item.text" [entries]="entries()" /></p>
                <p class="mt-0.5 text-xs/5 text-gray-600 dark:text-gray-400">
                  @if (item.pinned) {
                    <span class="font-medium text-primary-accessible dark:text-primary-50">Pinned</span> ·
                  }
                  {{ provenance(item).text }}@if (provenance(item).sessionId; as sid) {
                    (<a [routerLink]="['/s', sid]" class="rounded-sm font-medium text-primary-accessible underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50">open it</a>)}@if (provenance(item).at) { · {{ at(provenance(item).at) | date: 'MMM d, y' }}}
                </p>
              </div>
              @if (canEdit()) {
                <button
                  type="button"
                  (click)="togglePin(item)"
                  [disabled]="pinning() === item.anchor"
                  [attr.aria-pressed]="item.pinned"
                  [attr.aria-label]="(item.pinned ? 'Unpin: ' : 'Pin: ') + item.text"
                  [title]="item.pinned ? 'Pinned: no save can drop it until it’s unpinned' : 'Pin, so no save can drop it'"
                  class="grid size-8 shrink-0 place-items-center rounded-xl transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:hover:bg-white/10"
                  [class]="item.pinned ? 'text-primary-accessible dark:text-primary-50' : 'text-gray-500 dark:text-gray-400'"
                >
                  <ng-icon [name]="item.pinned ? 'heroBookmarkSolid' : 'heroBookmark'" class="size-4" aria-hidden="true" />
                </button>
              }
            </li>
          }
        </ul>
      } @else {
        <p class="p-5 text-sm/6 text-gray-600 dark:text-gray-400">This file has no items.</p>
      }
    }
  `,
})
export class MemoryFileViewComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);
  private user = inject(UserService);

  readonly projectId = input.required<string>();
  readonly scope = input.required<MemoryScope>();
  readonly entry = input.required<MemoryEntry>();
  /** The scope's files, for resolving links. */
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly limits = input.required<MemoryLimits>();
  /** May edit, delete, pin and unpin (an editor of project memory, or anyone in their own). */
  readonly canEdit = input(false);
  /** May propose a change instead (a viewer of an active project's shared memory). */
  readonly canPropose = input(false);

  readonly edit = output<void>();
  readonly propose = output<void>();
  readonly remove = output<void>();

  protected readonly file = signal<MemoryFile | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly pinning = signal<string | null>(null);

  private readonly myEmail = computed(() => this.user.currentUser()?.email ?? null);
  protected readonly updated = computed(() => parseIso(this.entry().updated));
  protected readonly updatedBy = computed(() => {
    const e = this.entry();
    return e.updatedBy.toLowerCase() === (this.myEmail() ?? '').toLowerCase() ? 'you' : personLabel(e.updatedByName, e.updatedBy);
  });
  protected readonly nearCap = computed(() => (this.entry().tokens ?? 0) >= this.limits().fileSoftThresholdTokens);
  protected readonly people = computed(() => contributors(this.file()?.items.map(i => i.provenance) ?? []));
  protected readonly peopleTitle = computed(() => {
    const names = this.file()?.people ?? {};
    return this.people().map(email => names[email] || email).join(', ');
  });

  /** The file to read: a new save (version) re-reads it. */
  private readonly key = computed(() => `${this.projectId()}|${this.scope()}|${this.entry().slug}|${this.entry().version}`);

  constructor() {
    effect(() => {
      this.key();
      untracked(() => void this.load());
    });
  }

  protected at(iso: string): Date {
    return parseIso(iso);
  }

  protected provenance(item: MemoryItem) {
    return describeProvenance(item.provenance, this.file()?.people ?? {}, this.myEmail());
  }

  private async load(): Promise<void> {
    const key = this.key();
    this.loading.set(true);
    this.error.set(null);
    try {
      const file = await firstValueFrom(this.api.memoryFile(this.projectId(), this.scope(), this.entry().slug));
      if (key !== this.key()) return;
      this.file.set(file);
    } catch (err) {
      if (key !== this.key()) return;
      this.file.set(null);
      this.error.set(projectErrorMessage(err, 'This file could not be loaded.'));
    } finally {
      if (key === this.key()) this.loading.set(false);
    }
  }

  protected async togglePin(item: MemoryItem): Promise<void> {
    const file = this.file();
    if (!file) return;
    this.pinning.set(item.anchor);
    try {
      const call = item.pinned
        ? this.api.unpinMemoryItem(this.projectId(), this.scope(), file.slug, item.anchor)
        : this.api.pinMemoryItem(this.projectId(), this.scope(), file.slug, item.anchor);
      const { pinned } = await firstValueFrom(call);
      const set = new Set(pinned);
      this.file.update(f => (f ? { ...f, items: f.items.map(i => ({ ...i, pinned: set.has(i.anchor) })) } : f));
      this.toast.success(item.pinned ? 'Unpinned' : 'Pinned. No save can drop it until it’s unpinned.');
    } catch (err) {
      this.toast.error('That didn’t go through', projectErrorMessage(err));
    } finally {
      this.pinning.set(null);
    }
  }
}
