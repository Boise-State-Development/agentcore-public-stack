import { ChangeDetectionStrategy, Component, Injector, afterNextRender, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroLink } from '@ng-icons/heroicons/outline';
import { ToastService } from '../../services/toast/toast.service';
import { MemoryEntry, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { MemoryLinkPickerDialogComponent, MemoryLinkPickerData, MemoryLinkPickerResult } from './memory-link-picker-dialog.component';
import { MemoryMeterComponent } from './memory-meter.component';
import { estimateTokens, itemProblem, lintLine } from './memory-text';

/**
 * Edit a scope's `MEMORY.md` (shared-projects 2.8b). Unlike a file, the index may hold any
 * text: headings, a sentence of orientation, one line per file. Its links are checked as
 * a file's are (only new dead ones block), and its meter is what reaches a task.
 */
@Component({
  selector: 'app-memory-index-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe, NgIcon, MemoryMeterComponent],
  providers: [provideIcons({ heroLink })],
  host: { class: 'block' },
  template: `
    <form (submit)="$event.preventDefault(); save()" class="flex flex-col gap-4 p-5" novalidate>
      <div>
        <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Edit the index</h2>
        <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">
          Keep it to one short line per file, such as “- [[deadlines]] — dates the team has agreed”. Only about the first {{ budget() | number }} tokens reach a task.
        </p>
      </div>
      <div>
        <div class="flex items-end justify-between gap-3">
          <label for="memory-index-text" class="block text-sm/6 font-medium text-gray-900 dark:text-white">MEMORY.md</label>
          <button
            type="button"
            (click)="insertLink()"
            class="inline-flex items-center gap-1.5 rounded-2xl px-3 py-1 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-white/10"
          >
            <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
            Insert a link
          </button>
        </div>
        <textarea
          id="memory-index-text"
          rows="12"
          [value]="text()"
          (input)="text.set($any($event.target).value)"
          [attr.aria-invalid]="problem() ? true : null"
          [attr.aria-describedby]="problem() ? 'memory-index-problem' : null"
          class="mt-1.5 block w-full rounded-2xl border border-gray-300 bg-white px-3.5 py-2 font-mono text-[0.8125rem]/6 text-gray-900 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-white"
        ></textarea>
      </div>
      <div aria-live="polite">
        @if (problem(); as message) {
          <p id="memory-index-problem" class="rounded-xl bg-state-danger-50 px-3 py-2 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
        }
      </div>
      @if (serverError()) {
        <p role="alert" class="rounded-xl bg-state-danger-50 px-3 py-2 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ serverError() }}</p>
      }
      <div class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-100 pt-4 dark:border-gray-700">
        <app-memory-meter [tokens]="tokens()" [max]="budget()" label="Index size against what reaches a task" />
        <span class="flex gap-2">
          <button type="button" (click)="cancelled.emit()" class="rounded-2xl border border-gray-300 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
            Cancel
          </button>
          <button type="submit" [disabled]="saving() || !!problem()" class="rounded-2xl bg-primary-accessible px-4 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50">
            {{ saving() ? 'Saving…' : 'Save index' }}
          </button>
        </span>
      </div>
    </form>
  `,
})
export class MemoryIndexEditorComponent {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);
  private injector = inject(Injector);

  readonly projectId = input.required<string>();
  readonly scope = input.required<MemoryScope>();
  readonly content = input.required<string>();
  readonly entries = input.required<readonly MemoryEntry[]>();
  readonly budget = input.required<number>();

  readonly done = output<void>();
  readonly cancelled = output<void>();

  protected readonly text = signal('');
  protected readonly saving = signal(false);
  protected readonly serverError = signal<string | null>(null);
  private original = '';

  protected readonly tokens = computed(() => estimateTokens(this.text()));
  protected readonly problem = computed(() => itemProblem(this.text(), this.original, this.entries()));

  constructor() {
    effect(() => {
      const content = this.content();
      untracked(() => {
        this.original = content;
        this.text.set(content);
      });
    });
    afterNextRender(() => document.getElementById('memory-index-text')?.focus());
  }

  protected async insertLink(): Promise<void> {
    const area = document.getElementById('memory-index-text') as HTMLTextAreaElement | null;
    const start = area?.selectionStart ?? this.text().length;
    const end = area?.selectionEnd ?? start;
    const ref = this.dialog.open<MemoryLinkPickerResult, MemoryLinkPickerData>(MemoryLinkPickerDialogComponent, {
      autoFocus: '#memory-link-filter',
      data: { entries: this.entries() },
    });
    const slug = await firstValueFrom(ref.closed);
    if (slug) {
      const link = `[[${slug}]]`;
      this.text.update(t => t.slice(0, start) + link + t.slice(end));
      afterNextRender(
        () => {
          const el = document.getElementById('memory-index-text') as HTMLTextAreaElement | null;
          el?.focus();
          el?.setSelectionRange(start + link.length, start + link.length);
        },
        { injector: this.injector },
      );
    } else {
      area?.focus();
    }
  }

  protected async save(): Promise<void> {
    if (this.saving() || this.problem()) return;
    this.saving.set(true);
    this.serverError.set(null);
    try {
      const result = await firstValueFrom(this.api.saveMemoryIndex(this.projectId(), this.scope(), this.text()));
      const flagged = lintLine(result.lint);
      if (flagged) {
        this.toast.warning('Saved the index', `${flagged} Every task loads the index, so check it before moving on.`);
      } else {
        this.toast.success('Saved the index');
      }
      this.done.emit();
    } catch (err) {
      this.serverError.set(projectErrorMessage(err, 'The index didn’t save. Try again.'));
    } finally {
      this.saving.set(false);
    }
  }
}
