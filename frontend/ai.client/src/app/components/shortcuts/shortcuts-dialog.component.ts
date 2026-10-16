import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../dialog/dialog-shell.component';
import { FEATURES } from '../../services/features';
import { SHORTCUTS, isApplePlatform, shortcutKeys, shortcutSpoken } from './shortcuts';

interface ShortcutRow {
  id: string;
  description: string;
  keys: string[];
  spoken: string;
}

/**
 * The keyboard shortcuts sheet (Cmd/Ctrl+/, or "Keyboard shortcuts" in the
 * user menu). Listed straight from `SHORTCUTS`, so it can't advertise a key
 * that doesn't work, minus search when conversation search is off.
 */
@Component({
  selector: 'app-shortcuts-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Keyboard shortcuts" (closed)="dialogRef.close()">
      <ul role="list" class="divide-y divide-gray-200 dark:divide-gray-700">
        @for (row of rows; track row.id) {
          <li class="flex items-center justify-between gap-4 py-2.5">
            <span class="text-sm/6 text-gray-700 dark:text-gray-300">{{ row.description }}</span>
            <span class="sr-only">{{ row.spoken }}</span>
            <span class="flex shrink-0 gap-1 text-gray-600 dark:text-gray-300" aria-hidden="true">
              @for (key of row.keys; track $index) {
                <kbd class="shortcut-kbd">{{ key }}</kbd>
              }
            </span>
          </li>
        }
      </ul>
    </app-dialog-shell>
  `,
  styles: `
    .shortcut-kbd {
      display: inline-flex;
      min-width: 1.75rem;
      justify-content: center;
      border-radius: 0.375rem;
      /* Follows the text colour, so it reads in both themes without a dark rule. */
      border: 1px solid color-mix(in srgb, currentColor 35%, transparent);
      padding: 0.125rem 0.375rem;
      font-family: inherit;
      font-size: 0.75rem;
      line-height: 1rem;
    }
  `,
})
export class ShortcutsDialogComponent {
  protected readonly dialogRef = inject(DialogRef);
  private readonly searchOn = inject(FEATURES).conversationSearch;

  protected readonly rows: ShortcutRow[] = (() => {
    const apple = isApplePlatform();
    return SHORTCUTS.filter(s => s.id !== 'search' || this.searchOn).map(s => ({
      id: s.id,
      description: s.description,
      keys: shortcutKeys(s, apple),
      spoken: shortcutSpoken(s, apple),
    }));
  })();
}
