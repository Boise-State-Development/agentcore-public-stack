import { Component, ChangeDetectionStrategy, inject, computed, OnInit, signal, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroXMark } from '@ng-icons/heroicons/outline';
import { FileUploadService, formatBytes } from '../../services/file-upload';

/**
 * Storage quota warning banner component
 *
 * Displays a compact warning message above the chat input when the user
 * approaches their file storage quota. Shows at 80% (warning) and 90%+ (critical).
 *
 * Features:
 * - Only shows when files are attached (controlled by parent)
 * - Compact tab-like design that sits on top of the chat input
 * - Dismissible with X button (dismissed until quota changes)
 * - Link to file browser for quota management
 * - Accessible with proper ARIA attributes
 * - Light/dark mode support
 */
@Component({
  selector: 'app-storage-quota-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroExclamationTriangle, heroXMark })],
  template: `
    @if (shouldShow()) {
      <div class="flex justify-center mb-2">
        <div
          class="inline-flex items-center gap-1.5 px-3 py-1 text-xs rounded-lg border animate-fade-in bg-white dark:bg-slate-800"
          [class.border-amber-400]="severity() === 'warning'"
          [class.text-amber-700]="severity() === 'warning'"
          [class.dark:border-amber-500]="severity() === 'warning'"
          [class.dark:text-amber-300]="severity() === 'warning'"
          [class.border-red-400]="severity() === 'critical'"
          [class.text-red-700]="severity() === 'critical'"
          [class.dark:border-red-500]="severity() === 'critical'"
          [class.dark:text-red-300]="severity() === 'critical'"
          role="status"
          aria-live="polite"
        >
          <ng-icon
            name="heroExclamationTriangle"
            class="size-3 shrink-0"
          />
          <span class="font-medium">{{ messageText() }}</span>
          <a
            routerLink="/files"
            class="underline hover:no-underline"
          >
            Manage files
          </a>
          <button
            type="button"
            (click)="dismiss($event)"
            class="p-0.5 -mr-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            aria-label="Dismiss warning"
          >
            <ng-icon name="heroXMark" class="size-3" />
          </button>
        </div>
      </div>
    }
  `,
  styles: [`
    @keyframes fadeIn {
      from {
        opacity: 0;
        transform: translateY(4px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .animate-fade-in {
      animation: fadeIn 0.15s ease-out;
    }
  `]
})
export class StorageQuotaBannerComponent implements OnInit {
  private fileUploadService = inject(FileUploadService);

  /** Whether to show the banner (controlled by parent - only when files attached) */
  readonly showWhenFilesAttached = input(false);

  /** Whether the user has dismissed the banner */
  private dismissed = signal(false);

  /** Last known usage percentage (to detect changes and reset dismiss) */
  private lastUsagePercent = signal(0);

  /** Quota usage percentage */
  readonly quotaUsagePercent = this.fileUploadService.quotaUsagePercent;

  /** Quota data */
  readonly quota = this.fileUploadService.quota;

  /** Severity level based on usage percentage */
  readonly severity = computed(() => {
    const percent = this.quotaUsagePercent();
    if (percent >= 90) return 'critical';
    if (percent >= 80) return 'warning';
    return null;
  });

  /** Whether to show the banner */
  readonly shouldShow = computed(() => {
    const severity = this.severity();
    if (!severity) return false;
    if (this.dismissed()) return false;
    if (!this.showWhenFilesAttached()) return false;
    return true;
  });

  /** Message text */
  readonly messageText = computed(() => {
    const q = this.quota();
    if (!q) return '';

    const percent = this.quotaUsagePercent();
    const remaining = q.maxBytes - q.usedBytes;

    if (percent >= 100) {
      return `Storage full (${formatBytes(q.usedBytes)}/${formatBytes(q.maxBytes)}).`;
    }
    if (percent >= 90) {
      return `Storage almost full - ${formatBytes(remaining)} remaining.`;
    }
    return `${Math.round(percent)}% storage used - ${formatBytes(remaining)} remaining.`;
  });

  ngOnInit(): void {
    this.loadQuota();
  }

  /**
   * Load quota from the server
   */
  async loadQuota(): Promise<void> {
    try {
      await this.fileUploadService.loadQuota();

      // Reset dismiss if quota percentage changed significantly
      const currentPercent = this.quotaUsagePercent();
      const lastPercent = this.lastUsagePercent();

      // If quota increased (e.g., uploaded more files), show banner again
      if (currentPercent > lastPercent + 5) {
        this.dismissed.set(false);
      }

      this.lastUsagePercent.set(currentPercent);
    } catch (error) {
      console.error('Failed to load storage quota:', error);
    }
  }

  /**
   * Dismiss the banner
   */
  dismiss(event: Event): void {
    event.stopPropagation();
    this.dismissed.set(true);
  }
}
