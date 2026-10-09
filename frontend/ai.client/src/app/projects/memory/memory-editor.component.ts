import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { CdkDrag, CdkDragDrop, CdkDragHandle, CdkDropList, moveItemInArray } from '@angular/cdk/drag-drop';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroInformationCircle, heroLink, heroPlus, heroXMark } from '@ng-icons/heroicons/outline';
import { ToastService } from '../../services/toast/toast.service';
import { MemoryEntry, MemoryLimits, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryLinkPickerDialogComponent, MemoryLinkPickerData, MemoryLinkPickerResult } from './memory-link-picker-dialog.component';
import { MemoryMeterComponent } from './memory-meter.component';
import {
  FRONTMATTER_TOKEN_ESTIMATE,
  MAX_DESCRIPTION_CHARS,
  aliasProblem,
  estimateTokens,
  itemProblem,
  lintLine,
  renderItems,
  slugProblem,
  splitAliases,
} from './memory-text';

/**
 * - `edit`: change a file directly (an editor of project memory, or anyone in their own).
 * - `propose`: send a changed file to the project's editors for review (a viewer).
 * - `new` / `propose-new`: the same for a file that doesn't exist yet.
 */
export type MemoryEditorMode = 'edit' | 'propose' | 'new' | 'propose-new';

export interface MemoryEditorDone {
  slug: string;
  /** It went to review rather than into memory. */
  proposed: boolean;
}

interface DraftItem {
  /** A local key, stable while items move. */
  key: number;
  /** The anchor it was read with; null for an item added here. */
  anchor: string | null;
  text: string;
  /** Its text when the editor opened, so a link it already had never blocks the save. */
  original: string;
  pinned: boolean;
}

interface Problem {
  /** The element it belongs to (its message is that element's description), or null for the form. */
  target: string | null;
  message: string;
}

/**
 * The Memory tab's block editor (shared-projects §6, 2.8b): one text box per item, so a
 * member edits facts without ever seeing the file format, anchors or frontmatter.
 *
 * Each item keeps the anchor it was read with, so its history and provenance follow it
 * through edits and moves; a removed item goes to the archive when the file is saved, and a
 * pinned one can't be removed here. Description and aliases are ordinary fields.
 *
 * Keyboard: the drag handles are one tab stop (a roving tabindex). Arrow keys move between
 * them and Alt with an arrow key moves the item, announced in a live region. Each item's
 * link button opens the link picker, which inserts `[[name]]` at the caret. Problems the
 * editor can see (a dead new link, a taken name, an oversize file) are listed as you type
 * in a polite live region and tied to their field; anything only the server can judge comes
 * back as its 400 sentence, shown as an alert.
 */
@Component({
  selector: 'app-memory-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkDrag, CdkDragHandle, CdkDropList, DecimalPipe, NgIcon, MemoryMeterComponent],
  providers: [provideIcons({ heroInformationCircle, heroLink, heroPlus, heroXMark })],
  host: { class: 'block' },
  template: `
    <form (submit)="$event.preventDefault(); submit()" class="flex flex-col gap-6 p-5" [attr.aria-busy]="loading()" novalidate>
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
          @switch (mode()) {
            @case ('new') { New file }
            @case ('propose-new') { Propose a new file }
            @case ('propose') { Propose a change to <span class="font-mono">{{ entry()?.slug }}</span> }
            @default { Edit <span class="font-mono">{{ entry()?.slug }}</span> }
          }
        </h2>
        @if (proposing()) {
          <p class="mt-2 flex items-start gap-2 rounded-xl bg-gray-50 px-3 py-2 text-sm/6 text-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
            <ng-icon name="heroInformationCircle" class="mt-1 size-4 shrink-0" aria-hidden="true" />
            This goes to the project’s editors for review. Memory doesn’t change until one of them approves it, and you’ll be told when they decide.
          </p>
        }
      </div>

      @if (loading()) {
        <div class="h-32 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700"></div>
      } @else if (loadError()) {
        <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ loadError() }}</p>
      } @else {
        @if (isNew()) {
          <div>
            <label for="memory-editor-name" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Name</label>
            <input
              id="memory-editor-name"
              type="text"
              autocomplete="off"
              spellcheck="false"
              [value]="slug()"
              (input)="slug.set($any($event.target).value); touched.set(true)"
              [attr.aria-invalid]="problemFor('name') ? true : null"
              [attr.aria-describedby]="problemFor('name') ? 'memory-editor-name-help memory-editor-name-error' : 'memory-editor-name-help'"
              placeholder="for example deadlines or people/jane-doe"
              class="mt-1.5 block w-full rounded-2xl border border-gray-300 bg-white px-3.5 py-2 font-mono text-sm/6 text-gray-900 placeholder:font-sans placeholder:text-gray-500 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-400"
            />
            <p id="memory-editor-name-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Lowercase words joined by hyphens. Other files link to it as [[name]].</p>
            @if (problemFor('name'); as message) {
              <p id="memory-editor-name-error" class="mt-1 text-xs/5 font-medium text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
            }
          </div>
        }

        <div class="grid gap-4 sm:grid-cols-2">
          <div>
            <label for="memory-editor-description" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Description</label>
            <input
              id="memory-editor-description"
              type="text"
              [value]="description()"
              (input)="description.set($any($event.target).value)"
              [attr.aria-invalid]="problemFor('description') ? true : null"
              aria-describedby="memory-editor-description-help"
              class="mt-1.5 block w-full rounded-2xl border border-gray-300 bg-white px-3.5 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-white"
            />
            <p id="memory-editor-description-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
              Shown in the index, where it helps the assistant decide when to open this file. {{ description().length }}/{{ maxDescription }}
            </p>
          </div>
          <div>
            <label for="memory-editor-aliases" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Also known as</label>
            <input
              id="memory-editor-aliases"
              type="text"
              [value]="aliasesText()"
              (input)="aliasesText.set($any($event.target).value)"
              [attr.aria-invalid]="problemFor('aliases') ? true : null"
              [attr.aria-describedby]="problemFor('aliases') ? 'memory-editor-aliases-help memory-editor-aliases-error' : 'memory-editor-aliases-help'"
              placeholder="Comma-separated"
              class="mt-1.5 block w-full rounded-2xl border border-gray-300 bg-white px-3.5 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-400"
            />
            <p id="memory-editor-aliases-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Other names [[links]] can use to reach this file.</p>
            @if (problemFor('aliases'); as message) {
              <p id="memory-editor-aliases-error" class="mt-1 text-xs/5 font-medium text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
            }
          </div>
        </div>

        <fieldset>
          <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">Items</legend>
          <p class="text-xs/5 text-gray-600 dark:text-gray-400">One fact per item. Link to another file with [[name]], or use the link button.</p>
          <p id="memory-editor-reorder-help" class="sr-only">Up and down arrow keys move between items. Alt with an arrow key moves this item.</p>
          <ol cdkDropList (cdkDropListDropped)="drop($event)" class="mt-3 flex flex-col gap-2">
            @for (item of items(); track item.key; let i = $index) {
              <li cdkDrag class="flex items-start gap-2 rounded-xl bg-white dark:bg-gray-800">
                <button
                  type="button"
                  cdkDragHandle
                  [id]="'memory-handle-' + item.key"
                  [attr.tabindex]="i === activeHandle() ? 0 : -1"
                  (focus)="activeHandle.set(i)"
                  (keydown)="onHandleKey($event, i)"
                  [attr.aria-label]="'Move item ' + (i + 1) + ' of ' + items().length"
                  aria-describedby="memory-editor-reorder-help"
                  class="mt-2 cursor-grab rounded-md p-1 text-gray-500 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
                >
                  <svg class="size-4" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                    <circle cx="5.5" cy="3.5" r="1.25" /><circle cx="10.5" cy="3.5" r="1.25" /><circle cx="5.5" cy="8" r="1.25" />
                    <circle cx="10.5" cy="8" r="1.25" /><circle cx="5.5" cy="12.5" r="1.25" /><circle cx="10.5" cy="12.5" r="1.25" />
                  </svg>
                </button>
                <div class="min-w-0 flex-1">
                  <label [for]="'memory-item-' + item.key" class="sr-only">Item {{ i + 1 }}</label>
                  <textarea
                    [id]="'memory-item-' + item.key"
                    rows="1"
                    [value]="item.text"
                    (input)="setText(item.key, $any($event.target).value)"
                    [attr.aria-invalid]="problemFor('item-' + item.key) ? true : null"
                    [attr.aria-describedby]="describedBy(item)"
                    class="block min-h-10 w-full resize-none rounded-2xl border border-gray-300 bg-white px-3.5 py-2 text-sm/6 text-gray-900 [field-sizing:content] focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                  ></textarea>
                  @if (item.pinned) {
                    <p [id]="'memory-item-pinned-' + item.key" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Pinned, so it can’t be removed until it’s unpinned.</p>
                  }
                  @if (problemFor('item-' + item.key); as message) {
                    <p [id]="'memory-item-error-' + item.key" class="mt-1 text-xs/5 font-medium text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
                  }
                </div>
                <button
                  type="button"
                  (click)="insertLink(item.key)"
                  [attr.aria-label]="'Insert a link in item ' + (i + 1)"
                  title="Insert a link"
                  class="mt-1 grid size-9 shrink-0 place-items-center rounded-2xl text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
                >
                  <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  (click)="removeItem(item.key)"
                  [disabled]="item.pinned"
                  [attr.aria-label]="'Remove item ' + (i + 1)"
                  [title]="item.pinned ? 'Pinned: unpin it first' : 'Remove (it goes to the archive)'"
                  class="mt-1 grid size-9 shrink-0 place-items-center rounded-2xl text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
                >
                  <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
                </button>
              </li>
            }
          </ol>
          <button
            id="memory-editor-add"
            type="button"
            (click)="addItem()"
            class="mt-3 inline-flex items-center gap-1.5 rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10 dark:hover:text-white"
          >
            <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
            Add item
          </button>
        </fieldset>

        <div aria-live="polite" class="text-sm/6">
          @if (shownProblems().length) {
            <div class="rounded-xl bg-state-danger-50 px-3 py-2 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
              <p class="font-medium">{{ shownProblems().length === 1 ? 'One thing to fix before saving:' : shownProblems().length + ' things to fix before saving:' }}</p>
              <ul class="mt-1 list-disc pl-5">
                @for (p of shownProblems(); track $index) {
                  <li>{{ p.message }}</li>
                }
              </ul>
            </div>
          }
          <span class="sr-only">{{ announcement() }}</span>
        </div>
        @if (serverError()) {
          <p role="alert" class="rounded-xl bg-state-danger-50 px-3 py-2 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ serverError() }}</p>
        }

        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-100 pt-5 dark:border-gray-700">
          <span class="flex items-center gap-2 text-xs/5 text-gray-600 dark:text-gray-400">
            <app-memory-meter [tokens]="estimate()" [max]="limits().fileHardCapTokens" [soft]="limits().fileSoftThresholdTokens" [compact]="true" label="Estimated file size" />
            About {{ estimate() | number }} of {{ limits().fileHardCapTokens | number }} tokens
          </span>
          <span class="flex gap-2">
            <button
              type="button"
              (click)="cancelled.emit()"
              class="rounded-2xl border border-gray-300 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="submit"
              [disabled]="saving()"
              class="rounded-2xl bg-primary-accessible px-4 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {{ saving() ? 'Saving…' : submitLabel() }}
            </button>
          </span>
        </div>
      }
    </form>
  `,
})
export class MemoryEditorComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);
  private injector = inject(Injector);

  protected readonly maxDescription = MAX_DESCRIPTION_CHARS;

  readonly projectId = input.required<string>();
  readonly scope = input.required<MemoryScope>();
  readonly mode = input.required<MemoryEditorMode>();
  /** The file being changed; null for a new one. */
  readonly entry = input<MemoryEntry | null>(null);
  /** The scope's files, for links, names and aliases. */
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly limits = input.required<MemoryLimits>();

  readonly done = output<MemoryEditorDone>();
  readonly cancelled = output<void>();

  protected readonly loading = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly saving = signal(false);
  protected readonly serverError = signal<string | null>(null);
  protected readonly slug = signal('');
  protected readonly description = signal('');
  protected readonly aliasesText = signal('');
  protected readonly items = signal<DraftItem[]>([]);
  protected readonly activeHandle = signal(0);
  protected readonly announcement = signal('');
  /** Problems a member hasn't had a chance to cause yet (a missing name) wait for a first try. */
  protected readonly touched = signal(false);
  private readonly attempted = signal(false);
  private nextKey = 0;

  protected readonly isNew = computed(() => this.mode() === 'new' || this.mode() === 'propose-new');
  protected readonly proposing = computed(() => this.mode() === 'propose' || this.mode() === 'propose-new');
  protected readonly submitLabel = computed(() => {
    if (this.proposing()) return 'Send proposal';
    if (this.isNew()) return 'Create file';
    return `Save as version ${(this.entry()?.version || 1) + 1}`;
  });
  private readonly fileSlug = computed(() => (this.isNew() ? this.slug().trim() : (this.entry()?.slug ?? '')));
  private readonly kept = computed(() => this.items().filter(i => i.text.trim()));
  protected readonly estimate = computed(
    () => estimateTokens(renderItems(this.kept().map(i => ({ text: i.text, anchor: i.anchor })))) + FRONTMATTER_TOKEN_ESTIMATE,
  );

  private readonly problems = computed<Problem[]>(() => {
    const list: Problem[] = [];
    const entries = this.entries();
    if (this.isNew()) {
      const message = slugProblem(this.slug(), entries);
      if (message) list.push({ target: 'name', message });
    }
    if (this.description().trim().length > MAX_DESCRIPTION_CHARS) {
      list.push({ target: 'description', message: `The description is over ${MAX_DESCRIPTION_CHARS} characters.` });
    }
    const aliases = aliasProblem(splitAliases(this.aliasesText()), this.fileSlug(), entries);
    if (aliases) list.push({ target: 'aliases', message: aliases });
    for (const item of this.items()) {
      const message = item.text.trim() ? itemProblem(item.text, item.original, entries) : null;
      if (message) list.push({ target: `item-${item.key}`, message });
    }
    if (!this.kept().length) list.push({ target: null, message: 'Add at least one item.' });
    if (this.estimate() > this.limits().fileHardCapTokens) {
      list.push({ target: null, message: 'This is over the size limit for one file. Move some items to another file.' });
    }
    return list;
  });

  /** Before a first save attempt, a missing name or an empty file isn't nagged about. */
  protected readonly shownProblems = computed(() =>
    this.attempted()
      ? this.problems()
      : this.problems().filter(p => !(p.target === null && p.message === 'Add at least one item.') && (p.target !== 'name' || this.touched())),
  );

  private readonly key = computed(() => `${this.projectId()}|${this.scope()}|${this.mode()}|${this.entry()?.slug ?? ''}`);

  constructor() {
    effect(() => {
      this.key();
      untracked(() => void this.load());
    });
  }

  protected problemFor(target: string): string | null {
    return this.shownProblems().find(p => p.target === target)?.message ?? null;
  }

  protected describedBy(item: DraftItem): string | null {
    const ids = [
      item.pinned ? `memory-item-pinned-${item.key}` : '',
      this.problemFor(`item-${item.key}`) ? `memory-item-error-${item.key}` : '',
    ].filter(Boolean);
    return ids.length ? ids.join(' ') : null;
  }

  private async load(): Promise<void> {
    const key = this.key();
    this.serverError.set(null);
    this.attempted.set(false);
    this.touched.set(false);
    this.activeHandle.set(0);
    const entry = this.entry();
    if (this.isNew() || !entry) {
      this.slug.set('');
      this.description.set('');
      this.aliasesText.set('');
      this.items.set([this.draft('', null, false)]);
      this.loading.set(false);
      return;
    }
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const file = await firstValueFrom(this.api.memoryFile(this.projectId(), this.scope(), entry.slug));
      if (key !== this.key()) return;
      this.description.set(file.description ?? '');
      this.aliasesText.set(entry.aliases.join(', '));
      this.items.set(file.items.map(i => this.draft(i.text, i.anchor, i.pinned)));
    } catch (err) {
      if (key === this.key()) this.loadError.set(projectErrorMessage(err, 'This file could not be opened for editing.'));
    } finally {
      if (key === this.key()) this.loading.set(false);
    }
  }

  private draft(text: string, anchor: string | null, pinned: boolean): DraftItem {
    return { key: this.nextKey++, anchor, text, original: text, pinned };
  }

  // ---- items ------------------------------------------------------------

  protected setText(key: number, text: string): void {
    this.items.update(list => list.map(i => (i.key === key ? { ...i, text } : i)));
  }

  protected addItem(): void {
    const item = this.draft('', null, false);
    this.items.update(list => [...list, item]);
    this.focusLater(`memory-item-${item.key}`);
  }

  protected removeItem(key: number): void {
    const list = this.items();
    const index = list.findIndex(i => i.key === key);
    const item = list[index];
    if (!item || item.pinned) return;
    this.items.set(list.filter(i => i.key !== key));
    this.activeHandle.set(Math.max(0, Math.min(this.activeHandle(), this.items().length - 1)));
    this.announcement.set(
      item.anchor ? `Item ${index + 1} removed. It goes to the archive when you save.` : `Item ${index + 1} removed.`,
    );
    const next = this.items()[Math.min(index, this.items().length - 1)];
    this.focusLater(next ? `memory-item-${next.key}` : 'memory-editor-add');
  }

  protected drop(event: CdkDragDrop<unknown>): void {
    if (event.previousIndex === event.currentIndex) return;
    this.move(event.previousIndex, event.currentIndex);
  }

  /** Arrow keys rove between handles; Alt with an arrow moves the item, focus riding along. */
  protected onHandleKey(event: KeyboardEvent, index: number): void {
    const last = this.items().length - 1;
    let target: number | null = null;
    switch (event.key) {
      case 'ArrowUp':
        target = index - 1;
        break;
      case 'ArrowDown':
        target = index + 1;
        break;
      case 'Home':
        target = event.altKey ? null : 0;
        break;
      case 'End':
        target = event.altKey ? null : last;
        break;
      default:
        return;
    }
    if (target === null || target < 0 || target > last) return;
    event.preventDefault();
    if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      this.move(index, target);
      return;
    }
    this.activeHandle.set(target);
    this.focusLater(`memory-handle-${this.items()[target].key}`);
  }

  private move(from: number, to: number): void {
    const list = [...this.items()];
    moveItemInArray(list, from, to);
    this.items.set(list);
    this.activeHandle.set(to);
    this.announcement.set(`Item moved to position ${to + 1} of ${list.length}.`);
    this.focusLater(`memory-handle-${list[to].key}`);
  }

  protected async insertLink(key: number): Promise<void> {
    const ref = this.dialog.open<MemoryLinkPickerResult, MemoryLinkPickerData>(MemoryLinkPickerDialogComponent, {
      autoFocus: '#memory-link-filter',
      data: { entries: this.entries(), current: this.isNew() ? null : this.fileSlug() },
    });
    const id = `memory-item-${key}`;
    const area = document.getElementById(id) as HTMLTextAreaElement | null;
    const start = area?.selectionStart ?? area?.value.length ?? 0;
    const end = area?.selectionEnd ?? start;
    const slug = await firstValueFrom(ref.closed);
    if (!slug) {
      this.focusLater(id);
      return;
    }
    const link = `[[${slug}]]`;
    const item = this.items().find(i => i.key === key);
    if (!item) return;
    this.setText(key, item.text.slice(0, start) + link + item.text.slice(end));
    afterNextRender(
      () => {
        const el = document.getElementById(id) as HTMLTextAreaElement | null;
        el?.focus();
        el?.setSelectionRange(start + link.length, start + link.length);
      },
      { injector: this.injector },
    );
  }

  private focusLater(id: string): void {
    afterNextRender(() => document.getElementById(id)?.focus(), { injector: this.injector });
  }

  // ---- save -------------------------------------------------------------

  protected async submit(): Promise<void> {
    this.attempted.set(true);
    this.serverError.set(null);
    if (this.problems().length || this.saving()) {
      const first = this.problems().find(p => p.target);
      if (first) this.focusLater(this.elementFor(first.target!));
      return;
    }
    const slug = this.fileSlug();
    const items = this.kept().map(i => ({ text: i.text.trim(), anchor: i.anchor }));
    const description = this.description().trim().replace(/\s+/g, ' ');
    const aliases = splitAliases(this.aliasesText());
    this.saving.set(true);
    try {
      if (this.proposing()) {
        const proposal = await firstValueFrom(
          this.api.proposeMemoryChange(this.projectId(), { slug, text: renderItems(items), description, aliases }),
        );
        const flagged = lintLine(proposal.lint);
        if (flagged) {
          this.toast.warning('Proposal sent, with a flag', `${flagged} Its reviewers will see that too.`);
        } else {
          this.toast.success('Proposal sent', 'The project’s editors will review it. You’ll be told when they decide.');
        }
      } else {
        const result = await firstValueFrom(
          this.api.saveMemoryFile(this.projectId(), this.scope(), slug, {
            items,
            description,
            aliases,
            baseVersion: this.isNew() ? 0 : this.entry()?.version || 1,
          }),
        );
        const title = this.isNew() ? `Created “${result.slug}”` : `Saved “${result.slug}” as version ${result.version}`;
        const flagged = lintLine(result.lint);
        if (flagged) {
          // The content check (2.7) only flags: the save went through, and the file shows the flag.
          this.toast.warning(title, `${flagged} It’s marked in the file.`);
        } else {
          this.toast.success(
            title,
            result.indexed === 'over_budget'
              ? 'The index is full, so it wasn’t added there. Edit the index to make room, or the assistant won’t see it first.'
              : result.warnings[0],
          );
        }
      }
      this.done.emit({ slug, proposed: this.proposing() });
    } catch (err) {
      this.serverError.set(projectErrorMessage(err, 'That didn’t save. Try again.'));
    } finally {
      this.saving.set(false);
    }
  }

  private elementFor(target: string): string {
    if (target === 'name') return 'memory-editor-name';
    if (target === 'description') return 'memory-editor-description';
    if (target === 'aliases') return 'memory-editor-aliases';
    return target.replace(/^item-/, 'memory-item-');
  }
}
