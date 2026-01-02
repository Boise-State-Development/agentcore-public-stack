import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
  inject,
  OnInit
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroTrash,
  heroArrowPath,
  heroExclamationTriangle,
  heroArrowLeft,
  heroDocument,
  heroArrowsUpDown,
  heroChevronDown,
  heroDocumentText,
  heroTableCells
} from '@ng-icons/heroicons/outline';
import {
  FileUploadService,
  FileMetadata,
  QuotaResponse,
  formatBytes
} from '../services/file-upload';
import { ToastService } from '../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../components/tooltip';

/** Maximum number of files that can be selected for bulk delete */
const MAX_SELECTION = 20;

/** File type icons and colors */
const FILE_TYPE_CONFIG: Record<string, { icon: string; color: string; label: string }> = {
  'application/pdf': { icon: 'heroDocument', color: 'text-red-600 dark:text-red-400', label: 'PDF' },
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': { icon: 'heroDocumentText', color: 'text-blue-600 dark:text-blue-400', label: 'DOCX' },
  'text/plain': { icon: 'heroDocumentText', color: 'text-gray-600 dark:text-gray-400', label: 'TXT' },
  'text/html': { icon: 'heroDocumentText', color: 'text-orange-600 dark:text-orange-400', label: 'HTML' },
  'text/csv': { icon: 'heroTableCells', color: 'text-green-600 dark:text-green-400', label: 'CSV' },
  'application/vnd.ms-excel': { icon: 'heroTableCells', color: 'text-green-600 dark:text-green-400', label: 'XLS' },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': { icon: 'heroTableCells', color: 'text-green-600 dark:text-green-400', label: 'XLSX' },
  'text/markdown': { icon: 'heroDocumentText', color: 'text-purple-600 dark:text-purple-400', label: 'MD' },
};

type SortBy = 'date' | 'size' | 'type';
type SortOrder = 'asc' | 'desc';

@Component({
  selector: 'app-file-browser-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, TooltipDirective],
  providers: [
    provideIcons({
      heroTrash,
      heroArrowPath,
      heroExclamationTriangle,
      heroArrowLeft,
      heroDocument,
      heroArrowsUpDown,
      heroChevronDown,
      heroDocumentText,
      heroTableCells
    })
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8">
        <!-- Header -->
        <div class="mb-8">
          <button
            type="button"
            (click)="goBack()"
            class="mb-4 flex items-center gap-2 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
          >
            <ng-icon name="heroArrowLeft" class="size-4" />
            Back
          </button>
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">My Files</h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            Manage your uploaded files. Select files to delete and free up storage space.
          </p>
        </div>

        <!-- Quota Bar -->
        @if (quota()) {
          <div class="mb-6 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm/6 font-medium text-gray-900 dark:text-white">Storage Used</span>
              <span class="text-sm/6 text-gray-600 dark:text-gray-400">
                {{ formatBytes(quota()!.usedBytes) }} of {{ formatBytes(quota()!.maxBytes) }}
              </span>
            </div>
            <div class="h-2 w-full rounded-full bg-gray-200 dark:bg-gray-700">
              <div
                class="h-2 rounded-full transition-all duration-300"
                [class]="quotaBarColor()"
                [style.width.%]="quotaUsagePercent()"
              ></div>
            </div>
            @if (quotaUsagePercent() >= 80) {
              <p class="mt-2 text-xs/5 text-amber-600 dark:text-amber-400">
                @if (quotaUsagePercent() >= 90) {
                  Storage almost full. Consider deleting unused files.
                } @else {
                  Approaching storage limit.
                }
              </p>
            }
          </div>
        }

        <!-- Selection Info Bar -->
        <div class="mb-6 flex flex-wrap items-center justify-between gap-4 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
          <div class="flex items-center gap-3">
            <span class="text-sm/6 font-medium text-gray-900 dark:text-white">
              {{ selectedCount() }} of {{ maxSelection }} selected
            </span>
            @if (selectedCount() > 0) {
              <button
                type="button"
                (click)="clearSelection()"
                class="text-sm/6 font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
              >
                Clear selection
              </button>
            }
          </div>
          <div class="flex items-center gap-3">
            <!-- Sort dropdown -->
            <div class="relative">
              <button
                type="button"
                (click)="toggleSortDropdown()"
                class="flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              >
                <ng-icon name="heroArrowsUpDown" class="size-4" />
                Sort: {{ sortByLabel() }}
                <ng-icon name="heroChevronDown" class="size-3" />
              </button>
              @if (sortDropdownOpen()) {
                <div class="absolute right-0 z-10 mt-1 w-48 rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800">
                  <div class="py-1">
                    @for (option of sortOptions; track option.value) {
                      <button
                        type="button"
                        (click)="setSortBy(option.value)"
                        class="flex w-full items-center justify-between px-4 py-2 text-sm/6 text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
                        [class.bg-gray-100]="sortBy() === option.value"
                        [class.dark:bg-gray-700]="sortBy() === option.value"
                      >
                        {{ option.label }}
                        @if (sortBy() === option.value) {
                          <span class="text-xs text-gray-500">{{ sortOrder() === 'asc' ? '↑' : '↓' }}</span>
                        }
                      </button>
                    }
                  </div>
                </div>
              }
            </div>

            <button
              type="button"
              (click)="refresh()"
              [disabled]="isLoading()"
              class="flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              [appTooltip]="'Refresh file list'"
              appTooltipPosition="top"
            >
              <ng-icon name="heroArrowPath" class="size-4" [class.animate-spin]="isLoading()" />
              Refresh
            </button>
            <button
              type="button"
              (click)="confirmBulkDelete()"
              [disabled]="selectedCount() === 0 || isDeleting()"
              class="flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm/6 font-medium text-white transition-colors hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-red-500 dark:hover:bg-red-600"
              [appTooltip]="'Delete selected files'"
              appTooltipPosition="top"
            >
              @if (isDeleting()) {
                <div class="size-4 animate-spin rounded-full border-2 border-white/30 border-t-white"></div>
                Deleting...
              } @else {
                <ng-icon name="heroTrash" class="size-4" />
                Delete Selected
              }
            </button>
          </div>
        </div>

        <!-- Selection Limit Warning -->
        @if (isAtSelectionLimit()) {
          <div class="mb-6 rounded-lg border border-yellow-200 bg-yellow-50 p-4 dark:border-yellow-800 dark:bg-yellow-900/20">
            <div class="flex items-center gap-3">
              <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-yellow-600 dark:text-yellow-400" />
              <p class="text-sm/6 text-yellow-700 dark:text-yellow-300">
                Selection limit reached. You can delete up to {{ maxSelection }} files at a time.
              </p>
            </div>
          </div>
        }

        <!-- Loading State -->
        @if (isLoading() && files().length === 0) {
          <div class="flex items-center justify-center py-12">
            <div class="text-center">
              <div class="mb-4 inline-block size-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent"></div>
              <p class="text-base/7 text-gray-600 dark:text-gray-400">Loading files...</p>
            </div>
          </div>
        } @else if (files().length === 0) {
          <!-- Empty State -->
          <div class="rounded-lg border border-gray-200 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
            <ng-icon name="heroDocument" class="mx-auto size-12 text-gray-400" />
            <h3 class="mt-4 text-base/7 font-semibold text-gray-900 dark:text-white">No files</h3>
            <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
              You haven't uploaded any files yet. Attach files to your conversations to see them here.
            </p>
          </div>
        } @else {
          <!-- Files List -->
          <fieldset class="rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <legend class="sr-only">Select files to delete</legend>
            <div class="divide-y divide-gray-200 dark:divide-gray-700">
              @for (file of files(); track file.uploadId) {
                @let isSelected = selectedFileIds().has(file.uploadId);
                @let isDisabled = !isSelected && isAtSelectionLimit();
                @let typeConfig = getFileTypeConfig(file.mimeType);
                <div class="relative flex gap-4 px-4 py-4">
                  <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gray-100 dark:bg-gray-700">
                    <ng-icon [name]="typeConfig.icon" class="size-5" [class]="typeConfig.color" />
                  </div>
                  <div class="min-w-0 flex-1">
                    <label
                      [for]="'file-' + file.uploadId"
                      class="block cursor-pointer"
                      [class.cursor-not-allowed]="isDisabled"
                      [class.opacity-50]="isDisabled"
                    >
                      <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ file.filename }}
                      </span>
                      <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs/5 text-gray-500 dark:text-gray-400">
                        <span class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                          {{ typeConfig.label }}
                        </span>
                        <span>{{ formatBytes(file.sizeBytes) }}</span>
                        <span>{{ formatDate(file.createdAt) }}</span>
                        <a
                          [routerLink]="['/s', file.sessionId]"
                          class="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
                          (click)="$event.stopPropagation()"
                        >
                          View conversation
                        </a>
                      </div>
                    </label>
                  </div>
                  <div class="flex h-6 shrink-0 items-center">
                    <div class="group grid size-4 grid-cols-1">
                      <input
                        [id]="'file-' + file.uploadId"
                        type="checkbox"
                        [checked]="isSelected"
                        [disabled]="isDisabled"
                        (change)="toggleFile(file.uploadId)"
                        class="col-start-1 row-start-1 appearance-none rounded-xs border border-gray-300 bg-white checked:border-indigo-600 checked:bg-indigo-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-100 disabled:checked:bg-gray-100 dark:border-white/10 dark:bg-white/5 dark:checked:border-indigo-500 dark:checked:bg-indigo-500 dark:focus-visible:outline-indigo-500 dark:disabled:border-white/5 dark:disabled:bg-white/10 dark:disabled:checked:bg-white/10 forced-colors:appearance-auto"
                      />
                      <svg viewBox="0 0 14 14" fill="none" class="pointer-events-none col-start-1 row-start-1 size-3.5 self-center justify-self-center stroke-white group-has-disabled:stroke-gray-950/25 dark:group-has-disabled:stroke-white/25">
                        <path d="M3 8L6 11L11 3.5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="opacity-0 group-has-checked:opacity-100" />
                      </svg>
                    </div>
                  </div>
                </div>
              }
            </div>
          </fieldset>

          <!-- Load More -->
          @if (hasMoreFiles()) {
            <div class="mt-6 text-center">
              <button
                type="button"
                (click)="loadMore()"
                [disabled]="isLoadingMore()"
                class="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              >
                @if (isLoadingMore()) {
                  <div class="size-4 animate-spin rounded-full border-2 border-gray-400 border-t-gray-700"></div>
                  Loading...
                } @else {
                  Load More
                }
              </button>
            </div>
          }
        }
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));
  `
})
export class FileBrowserPage implements OnInit {
  private fileUploadService = inject(FileUploadService);
  private toastService = inject(ToastService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  /** Maximum number of files that can be selected */
  readonly maxSelection = MAX_SELECTION;

  /** Sort options for dropdown */
  readonly sortOptions: { value: SortBy; label: string }[] = [
    { value: 'date', label: 'Date' },
    { value: 'size', label: 'Size' },
    { value: 'type', label: 'Type' }
  ];

  /** All loaded files */
  readonly files = signal<FileMetadata[]>([]);

  /** User's quota */
  readonly quota = signal<QuotaResponse | null>(null);

  /** Set of selected file IDs */
  readonly selectedFileIds = signal<Set<string>>(new Set());

  /** Loading states */
  readonly isLoading = signal(false);
  readonly isLoadingMore = signal(false);
  readonly isDeleting = signal(false);

  /** Sorting state */
  readonly sortBy = signal<SortBy>('date');
  readonly sortOrder = signal<SortOrder>('desc');
  readonly sortDropdownOpen = signal(false);

  /** Pagination cursor for loading more */
  private nextCursor = signal<string | null>(null);

  /** Number of selected files */
  readonly selectedCount = computed(() => this.selectedFileIds().size);

  /** Whether selection limit is reached */
  readonly isAtSelectionLimit = computed(() => this.selectedCount() >= this.maxSelection);

  /** Whether there are more files to load */
  readonly hasMoreFiles = computed(() => this.nextCursor() !== null);

  /** Quota usage percentage */
  readonly quotaUsagePercent = computed(() => {
    const q = this.quota();
    if (!q || q.maxBytes === 0) return 0;
    return Math.min(100, (q.usedBytes / q.maxBytes) * 100);
  });

  /** Quota bar color based on usage */
  readonly quotaBarColor = computed(() => {
    const percent = this.quotaUsagePercent();
    if (percent >= 90) return 'bg-red-600';
    if (percent >= 80) return 'bg-amber-500';
    return 'bg-blue-600';
  });

  /** Sort by label for dropdown button */
  readonly sortByLabel = computed(() => {
    const option = this.sortOptions.find(o => o.value === this.sortBy());
    return option?.label || 'Date';
  });

  ngOnInit(): void {
    this.loadFiles();
    this.loadQuota();
  }

  /**
   * Load files from the API
   */
  async loadFiles(): Promise<void> {
    this.isLoading.set(true);

    try {
      const response = await this.fileUploadService.listAllFiles({
        limit: 50,
        sortBy: this.sortBy(),
        sortOrder: this.sortOrder()
      });
      this.files.set(response.files);
      this.nextCursor.set(response.nextCursor);
    } catch (error) {
      console.error('Failed to load files:', error);
      this.toastService.error('Failed to load files');
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Load quota information
   */
  async loadQuota(): Promise<void> {
    try {
      const quota = await this.fileUploadService.loadQuota();
      this.quota.set(quota);
    } catch (error) {
      console.error('Failed to load quota:', error);
    }
  }

  /**
   * Load more files (pagination)
   */
  async loadMore(): Promise<void> {
    const cursor = this.nextCursor();
    if (!cursor || this.isLoadingMore()) return;

    this.isLoadingMore.set(true);

    try {
      const response = await this.fileUploadService.listAllFiles({
        limit: 50,
        cursor,
        sortBy: this.sortBy(),
        sortOrder: this.sortOrder()
      });
      this.files.update(current => [...current, ...response.files]);
      this.nextCursor.set(response.nextCursor);
    } catch (error) {
      console.error('Failed to load more files:', error);
      this.toastService.error('Failed to load more files');
    } finally {
      this.isLoadingMore.set(false);
    }
  }

  /**
   * Refresh the files list
   */
  async refresh(): Promise<void> {
    this.clearSelection();
    await Promise.all([this.loadFiles(), this.loadQuota()]);
  }

  /**
   * Toggle selection of a file
   */
  toggleFile(uploadId: string): void {
    this.selectedFileIds.update(ids => {
      const newIds = new Set(ids);
      if (newIds.has(uploadId)) {
        newIds.delete(uploadId);
      } else if (newIds.size < this.maxSelection) {
        newIds.add(uploadId);
      }
      return newIds;
    });
  }

  /**
   * Clear all selections
   */
  clearSelection(): void {
    this.selectedFileIds.set(new Set());
  }

  /**
   * Toggle sort dropdown
   */
  toggleSortDropdown(): void {
    this.sortDropdownOpen.update(open => !open);
  }

  /**
   * Set sort field and reload
   */
  setSortBy(value: SortBy): void {
    if (this.sortBy() === value) {
      // Toggle order if same field
      this.sortOrder.update(order => order === 'asc' ? 'desc' : 'asc');
    } else {
      this.sortBy.set(value);
      this.sortOrder.set('desc');
    }
    this.sortDropdownOpen.set(false);
    this.loadFiles();
  }

  /**
   * Show confirmation dialog and perform bulk delete
   */
  async confirmBulkDelete(): Promise<void> {
    const count = this.selectedCount();
    if (count === 0) return;

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Delete ${count} File${count === 1 ? '' : 's'}`,
        message: `Are you sure you want to delete ${count} file${count === 1 ? '' : 's'}? This action cannot be undone and will free up storage space.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true
      } as ConfirmationDialogData
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed !== true) return;

    await this.performBulkDelete();
  }

  /**
   * Perform the bulk delete operation
   */
  private async performBulkDelete(): Promise<void> {
    const uploadIds = Array.from(this.selectedFileIds());
    if (uploadIds.length === 0) return;

    this.isDeleting.set(true);

    try {
      const results = await this.fileUploadService.deleteFiles(uploadIds);

      // Remove deleted files from the list
      const deletedIds = new Set(
        results.filter(r => r.success).map(r => r.uploadId)
      );
      this.files.update(files =>
        files.filter(f => !deletedIds.has(f.uploadId))
      );

      // Clear selection
      this.clearSelection();

      // Reload quota
      await this.loadQuota();

      // Show result toast
      const successCount = results.filter(r => r.success).length;
      const failCount = results.filter(r => !r.success).length;

      if (failCount === 0) {
        this.toastService.success(
          'Files Deleted',
          `Successfully deleted ${successCount} file${successCount === 1 ? '' : 's'}.`
        );
      } else if (successCount > 0) {
        this.toastService.warning(
          'Partial Deletion',
          `Deleted ${successCount} file${successCount === 1 ? '' : 's'}, ${failCount} failed.`
        );
      } else {
        this.toastService.error(
          'Deletion Failed',
          `Failed to delete ${failCount} file${failCount === 1 ? '' : 's'}.`
        );
      }
    } catch (error) {
      console.error('Bulk delete failed:', error);
      this.toastService.error('Failed to delete files');
    } finally {
      this.isDeleting.set(false);
    }
  }

  /**
   * Navigate back to the previous page
   */
  goBack(): void {
    this.router.navigate(['/']);
  }

  /**
   * Get file type configuration for display
   */
  getFileTypeConfig(mimeType: string): { icon: string; color: string; label: string } {
    return FILE_TYPE_CONFIG[mimeType] || {
      icon: 'heroDocument',
      color: 'text-gray-600 dark:text-gray-400',
      label: 'FILE'
    };
  }

  /**
   * Format bytes to human-readable string
   */
  formatBytes = formatBytes;

  /**
   * Format a date string for display
   */
  formatDate(dateString: string): string {
    if (!dateString) return '';

    try {
      const date = new Date(dateString);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

      if (diffDays === 0) {
        return 'Today';
      } else if (diffDays === 1) {
        return 'Yesterday';
      } else if (diffDays < 7) {
        return `${diffDays} days ago`;
      } else {
        return date.toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined
        });
      }
    } catch {
      return '';
    }
  }
}
