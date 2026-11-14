import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPaperClip } from '@ng-icons/heroicons/outline';

import { ConfigService } from '../../../services/config.service';
import { downloadUrlFor } from '../../../shared/utils/file-download-url';
import { formatFileSize } from '../../services/handoff/handoff-prompt';

/** What a chip needs: the id to download, and what the file is. */
export interface FileChip {
  uploadId: string;
  filename: string;
  sizeBytes: number;
}

/**
 * Files shared by reference (handoffs spec §4.9), as chips on a delivery card and on the sent bubble.
 *
 * Each chip downloads through the durable, owner-scoped `/files/{id}/download` path, which mints a
 * fresh link per click, so it still works on a reload a week later. The file itself stays in the
 * conversation that made it; the receiving model reads it with its own tools.
 */
@Component({
  selector: 'app-delivery-file-chips',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroPaperClip })],
  template: `
    <ul class="flex flex-wrap gap-1.5" [attr.aria-label]="label()">
      @for (chip of chips(); track chip.uploadId) {
        <li>
          <a
            [href]="chip.href"
            class="inline-flex max-w-64 items-center gap-1 rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-800 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-white/10"
            [attr.aria-label]="'Download ' + chip.filename + ', ' + chip.size"
          >
            <ng-icon name="heroPaperClip" class="size-3.5 shrink-0" aria-hidden="true" />
            <span class="truncate">{{ chip.filename }}</span>
            <span class="shrink-0 text-gray-500 dark:text-gray-400">· {{ chip.size }}</span>
          </a>
        </li>
      }
    </ul>
  `,
})
export class DeliveryFileChipsComponent {
  private readonly config = inject(ConfigService);

  readonly files = input.required<readonly FileChip[]>();
  /** Names the list for assistive tech, e.g. "Files from Rubric for BIO 101". */
  readonly label = input('Files');

  protected readonly chips = computed(() =>
    this.files().map((f) => ({
      uploadId: f.uploadId,
      filename: f.filename || 'File',
      size: formatFileSize(f.sizeBytes),
      href: downloadUrlFor(this.config.appApiUrl(), f.uploadId),
    })),
  );
}
