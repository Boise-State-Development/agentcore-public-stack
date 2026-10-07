import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { MemoryEntry, MemoryScope } from '../models/project.model';
import { MemoryMeterComponent } from './memory-meter.component';
import { MemoryTextComponent } from './memory-text.component';
import { estimateTokens } from './memory-text';

interface IndexLine {
  kind: 'heading' | 'item' | 'text';
  text: string;
}

/**
 * A scope's `MEMORY.md`: the index that loads into every task in the project (or, for
 * "Just me", into the caller's own). The assistant sees it first and opens the files it
 * links to when they bear on a request, so its meter is measured against what reaches a
 * task, estimated at four characters a token as the harness cuts it.
 */
@Component({
  selector: 'app-memory-index-view',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MemoryMeterComponent, MemoryTextComponent],
  host: { class: 'block' },
  template: `
    <header class="border-b border-gray-200 p-5 dark:border-gray-700">
      <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">Index <span class="font-mono text-sm font-normal text-gray-600 dark:text-gray-400">MEMORY.md</span></h2>
      <p class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">
        {{ scope() === 'project'
          ? 'Loads into every member’s tasks in this project. The assistant opens the files it links to when they bear on a request.'
          : 'Loads into your own tasks in this project, beside the project’s index. Only you can see it.' }}
      </p>
      <div class="mt-4 text-xs/5 text-gray-600 dark:text-gray-400">
        <app-memory-meter [tokens]="tokens()" [max]="budget()" label="Index size against what reaches a task" />
      </div>
      @if (tokens() > budget()) {
        <p class="mt-3 rounded-xl bg-state-warning-50 px-3 py-2 text-xs/5 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-300">
          Only about the first {{ budget().toLocaleString('en-US') }} tokens reach a task. Lines past that point aren’t seen unless the assistant opens a file.
        </p>
      }
    </header>
    @if (lines().length) {
      <div class="space-y-1.5 p-5 text-sm/6 text-gray-900 dark:text-gray-100">
        @for (line of lines(); track $index) {
          @switch (line.kind) {
            @case ('heading') {
              <p class="pt-2 font-semibold first:pt-0"><app-memory-text [text]="line.text" [entries]="entries()" /></p>
            }
            @case ('item') {
              <p class="flex gap-2.5 break-words">
                <span class="mt-2.5 size-1.5 shrink-0 rounded-full bg-gray-500 dark:bg-gray-400" aria-hidden="true"></span>
                <span class="min-w-0"><app-memory-text [text]="line.text" [entries]="entries()" /></span>
              </p>
            }
            @default {
              <p class="break-words text-gray-700 dark:text-gray-300"><app-memory-text [text]="line.text" [entries]="entries()" /></p>
            }
          }
        }
      </div>
    } @else {
      <p class="p-5 text-sm/6 text-gray-600 dark:text-gray-400">
        The index is empty, so nothing from this memory loads into tasks yet. A file joins it when it’s saved by the assistant or approved from a proposal.
      </p>
    }
  `,
})
export class MemoryIndexViewComponent {
  readonly scope = input.required<MemoryScope>();
  readonly content = input.required<string>();
  readonly entries = input.required<readonly MemoryEntry[]>();
  /** Tokens of this index that reach a task. */
  readonly budget = input.required<number>();

  protected readonly tokens = computed(() => estimateTokens(this.content()));

  protected readonly lines = computed<IndexLine[]>(() =>
    this.content()
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map(raw => raw.trimEnd())
      .filter(raw => raw.trim().length > 0)
      .map(raw => {
        const heading = /^#{1,6}\s+(.*)$/.exec(raw);
        if (heading) return { kind: 'heading', text: heading[1] } as IndexLine;
        const item = /^\s*[-*]\s+(.*)$/.exec(raw);
        if (item) return { kind: 'item', text: item[1] } as IndexLine;
        return { kind: 'text', text: raw.trim() } as IndexLine;
      }),
  );
}
