import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroExclamationTriangle,
  heroFolder,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip';
import {
  type FolderAttachment,
  folderSkipReasons,
  folderStatusLabel,
} from '../../../services/folder-attachments';

/**
 * A Drive folder on the composer (drive-folder spec §4.7): one card however
 * many files it holds, with the expansion's progress, and Remove.
 *
 * `attaching` until app-api answers the attach, `attached` while the row is
 * known (its status drives the label), `error` when the attach itself failed.
 */
export interface ComposerFolder {
  /** Stable key for the card, before and after the row has an id. */
  localId: string;
  /** Known once app-api has created the row, or straight away for one restored from a draft. */
  folderAttachId: string | null;
  /** The conversation the folder is attached to. */
  sessionId: string;
  name: string;
  /** The connector's display name, for the card's second line. */
  source: string;
  row: FolderAttachment | null;
  phase: 'attaching' | 'attached' | 'error';
  error: string | null;
}

@Component({
  selector: 'app-folder-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroArrowPath, heroExclamationTriangle, heroFolder, heroXMark })],
  template: `
    <div
      class="group relative flex w-60 shrink-0 items-center gap-2 rounded-lg border px-3 py-2 transition-colors"
      [class]="isError() ? 'border-state-danger-200 bg-state-danger-50 dark:border-state-danger-800 dark:bg-state-danger-950/30' : 'border-gray-200 bg-gray-50 dark:border-gray-600 dark:bg-gray-800'"
      data-testid="folder-card"
    >
      <div
        class="flex size-10 shrink-0 items-center justify-center rounded-md border"
        [class]="isError() ? 'border-state-danger-300 bg-state-danger-100 dark:border-state-danger-700 dark:bg-state-danger-900/50' : 'border-primary-200 bg-primary-50 dark:border-primary-700 dark:bg-primary-900/40'"
      >
        @if (isError()) {
          <ng-icon name="heroExclamationTriangle" class="size-6 text-state-danger-500 dark:text-state-danger-400" aria-hidden="true" />
        } @else if (isBusy()) {
          <ng-icon name="heroArrowPath" class="size-6 animate-spin text-primary-700 dark:text-primary-300" aria-hidden="true" />
        } @else {
          <ng-icon name="heroFolder" class="size-6 text-primary-700 dark:text-primary-300" aria-hidden="true" />
        }
      </div>

      <div class="min-w-0 flex-1">
        <p class="truncate text-sm font-medium text-gray-900 dark:text-white">{{ folder().name }}</p>
        <p class="truncate text-xs text-gray-500 dark:text-gray-400" aria-live="polite">
          @if (isError()) {
            <span class="text-state-danger-500 dark:text-state-danger-400">{{ statusText() }}</span>
          } @else if (skipReasons()) {
            <span [appTooltip]="skipReasons()" appTooltipPosition="top">{{ statusText() }}</span>
            <span class="sr-only">. Left out: {{ skipReasons() }}</span>
          } @else {
            {{ statusText() }}
          }
        </p>
      </div>

      <div class="flex shrink-0 items-center gap-1">
        @if (needsConsent()) {
          <button
            type="button"
            (click)="onConnect($event)"
            class="rounded-md px-2 py-0.5 text-xs font-medium text-primary-700 ring-1 ring-primary-300 hover:bg-primary-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-300 dark:ring-primary-700 dark:hover:bg-primary-900/40"
          >
            Connect<span class="sr-only"> {{ folder().source }} to read {{ folder().name }}</span>
          </button>
        } @else if (canRetry()) {
          <button
            type="button"
            (click)="onRetry($event)"
            class="flex size-6 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-300"
            [appTooltip]="'Try again'"
            appTooltipPosition="top"
          >
            <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
            <span class="sr-only">Try reading {{ folder().name }} again</span>
          </button>
        }
        <button
          type="button"
          (click)="onRemove($event)"
          class="flex size-6 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
          [appTooltip]="'Remove folder'"
          appTooltipPosition="top"
        >
          <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          <span class="sr-only">Remove folder {{ folder().name }}</span>
        </button>
      </div>
    </div>
  `,
})
export class FolderCardComponent {
  readonly folder = input.required<ComposerFolder>();
  /** The card's `localId`. */
  readonly remove = output<string>();
  /** The card's `localId`: re-run a walk that stopped short. */
  readonly retry = output<string>();
  /** The card's `localId`: reconnect the folder's source, then resume. */
  readonly connect = output<string>();

  /** The source's token lapsed: Connect, not Try again, is what helps. */
  protected readonly needsConsent = computed(() => {
    const row = this.folder().row;
    return !!row && row.status !== 'expanding' && row.error === 'needs_consent';
  });

  protected readonly isError = computed(() => {
    const folder = this.folder();
    return folder.phase === 'error' || (folder.row?.status === 'failed' && (folder.row.counts.attached ?? 0) === 0);
  });

  protected readonly isBusy = computed(() => {
    const folder = this.folder();
    return folder.phase === 'attaching' || (folder.phase === 'attached' && (!folder.row || folder.row.status === 'expanding'));
  });

  protected readonly statusText = computed(() => {
    const folder = this.folder();
    if (folder.phase === 'attaching') return `Adding from ${folder.source}…`;
    if (folder.phase === 'error') return folder.error ?? "Couldn't attach this folder";
    if (!folder.row) return 'Checking…';
    return folderStatusLabel(folder.row);
  });

  protected readonly skipReasons = computed(() => {
    const row = this.folder().row;
    return row && row.status !== 'expanding' ? folderSkipReasons(row) : '';
  });

  /** A settled walk that stopped short for a reason another try can fix. */
  protected readonly canRetry = computed(() => {
    const row = this.folder().row;
    return !!row && row.status !== 'expanding' && !!row.error;
  });

  protected onRemove(event: Event): void {
    event.stopPropagation();
    this.remove.emit(this.folder().localId);
  }

  protected onConnect(event: Event): void {
    event.stopPropagation();
    this.connect.emit(this.folder().localId);
  }

  protected onRetry(event: Event): void {
    event.stopPropagation();
    this.retry.emit(this.folder().localId);
  }
}
