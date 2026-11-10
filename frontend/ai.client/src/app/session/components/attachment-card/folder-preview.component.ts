import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroPresentationChartBar,
  heroTableCells,
} from '@ng-icons/heroicons/outline';

/** Rows a folder's preview draws before its "+N more" line. */
const PREVIEW_ROWS = 5;

const ICON_BY_EXTENSION: Record<string, string> = {
  pdf: 'heroDocument',
  docx: 'heroDocumentText',
  doc: 'heroDocumentText',
  txt: 'heroDocumentText',
  md: 'heroDocumentText',
  html: 'heroCodeBracket',
  htm: 'heroCodeBracket',
  csv: 'heroTableCells',
  xlsx: 'heroTableCells',
  xls: 'heroTableCells',
  pptx: 'heroPresentationChartBar',
  png: 'heroPhoto',
  jpg: 'heroPhoto',
  jpeg: 'heroPhoto',
  gif: 'heroPhoto',
  webp: 'heroPhoto',
};

/**
 * The inside of a folder card's sheet: its first files, one per row with a
 * type icon from the extension, then "+N more". Decorative, since the card's
 * footer and its file list say the same in words.
 */
@Component({
  selector: 'app-folder-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroPresentationChartBar,
      heroTableCells,
    }),
  ],
  host: { class: 'block size-full', 'aria-hidden': 'true' },
  template: `
    @if (rows().length > 0) {
      <ul class="grid gap-px px-2.5 py-2 text-[10px]/4 text-gray-700 dark:text-gray-300">
        @for (row of rows(); track $index; let odd = $odd) {
          <li class="flex min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5" [class]="odd ? '' : 'bg-gray-50 dark:bg-white/5'">
            <ng-icon [name]="row.icon" size="11" class="shrink-0 text-gray-500 dark:text-gray-400" />
            <span class="truncate">{{ row.name }}</span>
          </li>
        }
        @if (more() > 0) {
          <li class="px-1.5 text-gray-500 dark:text-gray-400">+{{ more() }} more</li>
        }
      </ul>
    } @else {
      <div class="space-y-1.5 px-3 py-2.5">
        @for (width of skeleton; track $index) {
          <div class="h-1.5 rounded-full bg-gray-200 dark:bg-gray-700" [style.width.%]="width"></div>
        }
      </div>
    }
  `,
})
export class FolderPreviewComponent {
  /** Paths or names inside the folder, in the order to show them. */
  readonly files = input<readonly string[]>([]);
  /** Files beyond `files` the folder holds. */
  readonly moreCount = input(0);
  readonly limit = input(PREVIEW_ROWS);

  protected readonly skeleton = [86, 72, 90, 64];

  protected readonly rows = computed(() =>
    this.files()
      .slice(0, this.limit())
      .map((path) => {
        const name = path.split('/').pop() || path;
        const extension = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
        return { name, icon: ICON_BY_EXTENSION[extension] ?? 'heroDocument' };
      }),
  );

  protected readonly more = computed(
    () => Math.max(0, this.files().length - this.limit()) + this.moreCount(),
  );
}
