import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroTableCells,
} from '@ng-icons/heroicons/outline';

import type { SharedConversationArtifact } from '../../session/services/share/share.service';
import {
  SharedArtifactDialogComponent,
  type SharedArtifactDialogData,
} from './shared-artifact-dialog.component';

interface TypeStyle {
  readonly label: string;
  readonly icon: string;
  readonly bg: string;
  readonly text: string;
}

/** Same `filetype-*` identity tokens the library and attachment cards
 *  use, so one artifact reads as the same kind of thing everywhere. */
const TYPE_STYLES: Record<string, TypeStyle> = {
  'text/markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-600 dark:text-filetype-markdown-300',
  },
  'text/x-markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-600 dark:text-filetype-markdown-300',
  },
  'text/html': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-600 dark:text-filetype-code-300',
  },
  'application/xhtml+xml': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-600 dark:text-filetype-code-300',
  },
  'text/csv': {
    label: 'CSV',
    icon: 'heroTableCells',
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-600 dark:text-filetype-sheet-300',
  },
  'image/svg+xml': {
    label: 'SVG',
    icon: 'heroPhoto',
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
  },
};

const DEFAULT_TYPE_STYLE: TypeStyle = {
  label: 'Text',
  icon: 'heroDocument',
  bg: 'bg-gray-100 dark:bg-gray-700',
  text: 'text-gray-600 dark:text-gray-300',
};

/**
 * An artifact inside a shared conversation, as its recipient sees it.
 *
 * Deliberately a separate component from `ArtifactCardComponent` rather
 * than a mode of it. That one opens the docked owner panel and carries
 * download, share and rename/delete — all keyed on endpoints a
 * conversation-share recipient has no handle for. Sharing the markup
 * would mean guarding every one of those on a flag, and the failure
 * mode of a missed guard is a visible button that 403s.
 *
 * What is shared instead is the *viewer* underneath the dialog, which is
 * presentational and already serves three mint paths.
 */
@Component({
  selector: 'app-shared-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  viewProviders: [
    provideIcons({
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroTableCells,
    }),
  ],
  template: `
    <button
      type="button"
      (click)="open()"
      [attr.aria-label]="ariaLabel()"
      class="flex w-full items-center gap-3 rounded-2xl border border-gray-200 bg-white px-3 py-2.5 text-left transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700"
    >
      <span
        class="grid size-9 shrink-0 place-items-center rounded-2xl"
        [class]="style().bg"
        aria-hidden="true"
      >
        <ng-icon [name]="style().icon" class="size-5" [class]="style().text" />
      </span>

      <span class="min-w-0 flex-1">
        <span
          class="block truncate text-sm/6 font-semibold text-gray-900 dark:text-white"
        >
          {{ artifact().title || 'Untitled artifact' }}
        </span>
        <span class="block text-xs/5 text-gray-500 dark:text-gray-400">
          {{ style().label }}
          @if (artifact().version > 1) {
            <span aria-hidden="true"> · </span>v{{ artifact().version }}
          }
        </span>
      </span>
    </button>
  `,
})
export class SharedArtifactCardComponent {
  readonly artifact = input.required<SharedConversationArtifact>();
  /** The CONVERSATION share this came in on — the grant, and half the
   *  address the mint needs. */
  readonly shareId = input.required<string>();

  private readonly dialog = inject(Dialog);

  protected readonly style = computed<TypeStyle>(
    () =>
      TYPE_STYLES[
        this.artifact().contentType.split(';')[0].trim().toLowerCase()
      ] ?? DEFAULT_TYPE_STYLE,
  );

  protected readonly ariaLabel = computed(
    () =>
      `Open ${this.style().label} artifact ${
        this.artifact().title || 'Untitled'
      }, version ${this.artifact().version}`,
  );

  protected open(): void {
    const data: SharedArtifactDialogData = {
      shareId: this.shareId(),
      artifact: this.artifact(),
    };
    this.dialog.open<void>(SharedArtifactDialogComponent, { data });
  }
}
