import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MemoryEntry } from '../models/project.model';
import { linkParts } from './memory-text';

/**
 * Memory text with its `[[links]]` as chips. A link that resolves opens that file on the
 * Memory page (same scope); one that matches no file or alias is drawn dashed and says so
 * to a screen reader.
 */
@Component({
  selector: 'app-memory-text',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `@for (part of parts(); track $index) {@if (part.kind === 'text') {<span class="whitespace-pre-wrap">{{ part.text }}</span>} @else if (part.slug) {<a
          [routerLink]="[]"
          [queryParams]="{ file: part.slug, view: null }"
          queryParamsHandling="merge"
          class="mx-0.5 inline-flex items-center rounded-md border border-gray-200 bg-gray-50 px-1.5 align-baseline font-mono text-[0.8125rem] text-primary-accessible transition-colors hover:border-gray-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-primary-accessible-dark"
          >{{ part.name }}</a
        >} @else {<span class="mx-0.5 inline-flex items-center rounded-md border border-dashed border-state-danger-500 px-1.5 align-baseline font-mono text-[0.8125rem] text-state-danger-700 dark:text-state-danger-300"
          >{{ part.name }}<span class="sr-only"> (no file by that name)</span></span
        >}}`,
})
export class MemoryTextComponent {
  readonly text = input.required<string>();
  /** The files of the scope the text belongs to, to resolve its links against. */
  readonly entries = input.required<readonly MemoryEntry[]>();

  protected readonly parts = computed(() => linkParts(this.text(), this.entries()));
}
