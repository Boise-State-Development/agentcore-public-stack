import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { MemoryEntry } from '../models/project.model';
import { INDEX_SLUG } from './memory-text';

export interface MemoryLinkPickerData {
  /** The files a link in this scope can reach. */
  entries: readonly MemoryEntry[];
  /** The file being edited, which a link to itself would be pointless in. */
  current?: string | null;
}

/** The chosen file's name, or `undefined` when cancelled. */
export type MemoryLinkPickerResult = string | undefined;

/**
 * Pick a file to link to with `[[name]]` (shared-projects 2.8b).
 *
 * Open it with `autoFocus: '#memory-link-filter'`: by default CDK focuses the first
 * tabbable, which is the shell's Close button.
 *
 * The APG combobox pattern: the filter keeps focus while ArrowUp and ArrowDown move the
 * active option (`aria-activedescendant`), Enter picks it, Escape closes. The match count
 * is announced politely as the filter narrows. A file matches on its name, its aliases
 * or its description; the index can be linked like any file.
 */
@Component({
  selector: 'app-memory-link-picker-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  template: `
    <app-dialog-shell title="Link to a file" description="The link reads [[name]] and opens that file." (closed)="cancel()">
      <label for="memory-link-filter" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Find a file</label>
      <input
        id="memory-link-filter"
        type="text"
        role="combobox"
        autocomplete="off"
        aria-autocomplete="list"
        aria-expanded="true"
        aria-controls="memory-link-options"
        [attr.aria-activedescendant]="options().length ? optionId(active()) : null"
        [value]="query()"
        (input)="onInput($any($event.target).value)"
        (keydown)="onKeydown($event)"
        placeholder="Name, alias or description"
        class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400"
      />
      <p class="sr-only" aria-live="polite">{{ options().length }} {{ options().length === 1 ? 'file matches' : 'files match' }}</p>
      <ul id="memory-link-options" role="listbox" aria-label="Files" class="mt-3 max-h-72 space-y-0.5 overflow-y-auto">
        @for (option of options(); track option.slug; let i = $index) {
          <li
            [id]="optionId(i)"
            role="option"
            [attr.aria-selected]="i === active()"
            (click)="pick(option.slug)"
            (mousemove)="active.set(i)"
            class="cursor-pointer rounded-xl px-3 py-2"
            [class]="i === active() ? 'bg-gray-100 dark:bg-gray-700' : ''"
          >
            <span class="block font-mono text-[0.8125rem] font-medium text-gray-900 dark:text-white">{{ option.slug }}</span>
            @if (option.detail) {
              <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-300">{{ option.detail }}</span>
            }
          </li>
        } @empty {
          <li class="px-3 py-2 text-sm/6 text-gray-600 dark:text-gray-400">No file matches “{{ query() }}”.</li>
        }
      </ul>
    </app-dialog-shell>
  `,
})
export class MemoryLinkPickerDialogComponent {
  private dialogRef = inject<DialogRef<MemoryLinkPickerResult>>(DialogRef);
  private data = inject<MemoryLinkPickerData>(DIALOG_DATA);

  protected readonly query = signal('');
  protected readonly active = signal(0);

  protected readonly options = computed(() => {
    const q = this.query().trim().toLowerCase();
    const all = [
      { slug: INDEX_SLUG, detail: 'The index', haystack: 'memory.md index' },
      ...this.data.entries
        .filter(e => e.slug !== this.data.current)
        .map(e => ({
          slug: e.slug,
          detail: [e.description, e.aliases.length ? `also ${e.aliases.map(a => `“${a}”`).join(', ')}` : ''].filter(Boolean).join(' · '),
          haystack: [e.slug, ...e.aliases, e.description].join(' ').toLowerCase(),
        })),
    ];
    return q ? all.filter(o => o.haystack.includes(q)) : all;
  });

  protected optionId(i: number): string {
    return `memory-link-option-${i}`;
  }

  protected onInput(value: string): void {
    this.query.set(value);
    this.active.set(0);
  }

  protected onKeydown(event: KeyboardEvent): void {
    const count = this.options().length;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (count) this.active.update(i => (i + 1) % count);
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (count) this.active.update(i => (i - 1 + count) % count);
        break;
      case 'Home':
        event.preventDefault();
        this.active.set(0);
        break;
      case 'End':
        event.preventDefault();
        if (count) this.active.set(count - 1);
        break;
      case 'Enter': {
        event.preventDefault();
        const option = this.options()[this.active()];
        if (option) this.pick(option.slug);
        return;
      }
      default:
        return;
    }
    document.getElementById(this.optionId(this.active()))?.scrollIntoView?.({ block: 'nearest' });
  }

  protected pick(slug: string): void {
    this.dialogRef.close(slug);
  }

  protected cancel(): void {
    this.dialogRef.close(undefined);
  }
}
