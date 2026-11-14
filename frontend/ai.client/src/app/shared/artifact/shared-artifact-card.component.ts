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
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
} from '../../components/content-card/content-card.component';
import {
  ArtifactThumbnailComponent,
  type ArtifactThumbnailSource,
} from '../../artifacts/components/artifact-thumbnail.component';
import {
  SharedArtifactDialogComponent,
  type SharedArtifactDialogData,
} from './shared-artifact-dialog.component';

export interface TypeStyle {
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
    text: 'text-filetype-markdown-700 dark:text-filetype-markdown-300',
  },
  'text/x-markdown': {
    label: 'Markdown',
    icon: 'heroDocumentText',
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-700 dark:text-filetype-markdown-300',
  },
  'text/html': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-700 dark:text-filetype-code-300',
  },
  'application/xhtml+xml': {
    label: 'Web page',
    icon: 'heroCodeBracket',
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-700 dark:text-filetype-code-300',
  },
  'text/csv': {
    label: 'CSV',
    icon: 'heroTableCells',
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-700 dark:text-filetype-sheet-300',
  },
  'image/svg+xml': {
    label: 'SVG',
    icon: 'heroPhoto',
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-700 dark:text-filetype-image-300',
  },
};

const DEFAULT_TYPE_STYLE: TypeStyle = {
  label: 'Text',
  icon: 'heroDocument',
  bg: 'bg-gray-100 dark:bg-gray-700',
  text: 'text-gray-600 dark:text-gray-300',
};

/** How an artifact of ``contentType`` is labelled and coloured; shared with the project Outputs cards. */
export function artifactTypeStyle(contentType: string): TypeStyle {
  return TYPE_STYLES[contentType.split(';')[0].trim().toLowerCase()] ?? DEFAULT_TYPE_STYLE;
}

/**
 * An artifact inside a shared conversation, as its recipient sees it,
 * drawn with the same content card as the owner's artifact card: a live
 * miniature (minted through the conversation share) in the tray, the
 * title and type, and Open.
 *
 * Deliberately a separate component from `ArtifactCardComponent` rather
 * than a mode of it. That one opens the docked owner panel and carries
 * download, share and rename/delete — all keyed on endpoints a
 * conversation-share recipient has no handle for. Sharing the markup
 * would mean guarding every one of those on a flag, and the failure
 * mode of a missed guard is a visible button that 403s. So this card has
 * no overflow menu at all.
 *
 * What is shared instead is the card shell, the thumbnail and the
 * *viewer* underneath the dialog, which are presentational.
 */
@Component({
  selector: 'app-shared-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, ArtifactThumbnailComponent, NgIcon],
  viewProviders: [
    provideIcons({
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroTableCells,
    }),
  ],
  host: { class: 'block w-80 max-w-full' },
  template: `
    <app-content-card [title]="title()" [label]="cardLabel()" [meta]="meta()">
      <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1.5" aria-hidden="true">
        <span class="flex shrink-0" [class]="style().text">
          <ng-icon [name]="style().icon" size="13" />
        </span>
        <span
          class="min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-600 dark:bg-white/5 dark:text-gray-400"
          >{{ style().label }}</span
        >
      </div>

      <app-artifact-thumbnail contentCardPreview [source]="thumbnail()" />

      <button
        contentCardAction
        type="button"
        [class]="actionClass"
        [attr.aria-label]="ariaLabel()"
        (click)="open()"
      >
        Open
      </button>
    </app-content-card>
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

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;

  protected readonly title = computed(
    () => this.artifact().title || 'Untitled artifact',
  );

  /** The version only once there is more than one. */
  protected readonly meta = computed(() => {
    const { version } = this.artifact();
    return version > 1 ? `${this.style().label} · v${version}` : this.style().label;
  });

  protected readonly cardLabel = computed(
    () => `${this.style().label} artifact ${this.title()}`,
  );

  protected readonly thumbnail = computed<ArtifactThumbnailSource>(() => ({
    kind: 'conversation',
    shareId: this.shareId(),
    artifactId: this.artifact().artifactId,
    contentType: this.artifact().contentType,
  }));

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
