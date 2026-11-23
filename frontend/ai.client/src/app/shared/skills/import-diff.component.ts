import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ImportTextDiff } from './skill-bundle-import.model';

type DiffLineKind = 'added' | 'removed' | 'hunk' | 'header' | 'context';

interface DiffLine {
  kind: DiffLineKind;
  /** The line as the diff has it, its `+` / `-` marker included. */
  text: string;
}

/** Longer than this, the diff starts collapsed. */
const OPEN_BY_DEFAULT_MAX_LINES = 20;

const LINE_CLASSES: Record<DiffLineKind, string> = {
  added: 'bg-state-success-50 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-100',
  removed: 'bg-state-danger-50 text-state-danger-900 dark:bg-state-danger-900/30 dark:text-state-danger-100',
  hunk: 'text-gray-600 dark:text-gray-400',
  header: 'text-gray-600 dark:text-gray-400',
  context: 'text-gray-800 dark:text-gray-200',
};

const SCREEN_READER_PREFIX: Record<DiffLineKind, string> = {
  added: 'Added: ',
  removed: 'Removed: ',
  hunk: '',
  header: '',
  context: '',
};

/**
 * One field's unified diff in a re-import preview. Added and removed lines keep
 * their `+` / `-` marker and carry a screen-reader label, so the change reads
 * without colour. The text comes from a third-party bundle, so it is rendered by
 * interpolation only, never as markup or markdown.
 */
@Component({
  selector: 'app-import-diff',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (lines().length > 0) {
      <details class="mt-3" [open]="openByDefault()" data-testid="import-diff">
        <summary
          class="cursor-pointer text-sm/6 font-medium text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-white"
        >
          {{ label() }}: {{ addedCount() }} added, {{ removedCount() }} removed
        </summary>
        <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
          Lines starting with + are added; lines starting with - are removed.
        </p>
        <pre
          class="mt-1 max-h-96 overflow-auto rounded-lg border border-gray-200 bg-white py-1 font-mono text-xs/5 dark:border-gray-700 dark:bg-gray-900"
        ><code>@for (line of lines(); track $index) {<span class="block px-2 whitespace-pre-wrap break-words" [class]="lineClass(line)" [attr.data-kind]="line.kind"><span class="sr-only">{{ srPrefix(line) }}</span>{{ line.text }}</span>}</code></pre>
        @if (diff()?.truncated) {
          <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400" data-testid="import-diff-truncated">
            Only the first 400 lines of the diff are shown.
          </p>
        }
      </details>
    }
  `,
})
export class ImportDiffComponent {
  readonly label = input.required<string>();
  readonly diff = input<ImportTextDiff | null | undefined>(null);

  protected readonly lines = computed<DiffLine[]>(() => {
    const text = this.diff()?.diff ?? '';
    return text ? text.split('\n').map(classify) : [];
  });

  protected readonly addedCount = computed(
    () => this.lines().filter((l) => l.kind === 'added').length,
  );
  protected readonly removedCount = computed(
    () => this.lines().filter((l) => l.kind === 'removed').length,
  );
  protected readonly openByDefault = computed(
    () => !this.diff()?.truncated && this.lines().length <= OPEN_BY_DEFAULT_MAX_LINES,
  );

  protected lineClass(line: DiffLine): string {
    return LINE_CLASSES[line.kind];
  }

  protected srPrefix(line: DiffLine): string {
    return SCREEN_READER_PREFIX[line.kind];
  }
}

function classify(text: string): DiffLine {
  if (text.startsWith('+++') || text.startsWith('---')) return { kind: 'header', text };
  if (text.startsWith('@@')) return { kind: 'hunk', text };
  if (text.startsWith('+')) return { kind: 'added', text };
  if (text.startsWith('-')) return { kind: 'removed', text };
  return { kind: 'context', text };
}
