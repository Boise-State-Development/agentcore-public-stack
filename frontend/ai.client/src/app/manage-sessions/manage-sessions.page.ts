import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
  inject,
  OnInit
} from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroTrash,
  heroArrowPath,
  heroExclamationTriangle,
  heroArrowLeft,
  heroChatBubbleLeftRight
} from '@ng-icons/heroicons/outline';
import { SessionService } from '../session/services/session/session.service';
import { SessionMetadata } from '../session/services/models/session-metadata.model';
import { ToastService } from '../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData
} from '../components/confirmation-dialog/confirmation-dialog.component';

/** Maximum number of sessions that can be selected for bulk delete */
const MAX_SELECTION = 20;

@Component({
  selector: 'app-manage-sessions-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroTrash,
      heroArrowPath,
      heroExclamationTriangle,
      heroArrowLeft,
      heroChatBubbleLeftRight
    })
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8">
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
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Manage Conversations</h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            Select conversations to delete. You can delete up to {{ maxSelection }} conversations at a time.
          </p>
        </div>

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
            <button
              type="button"
              (click)="refresh()"
              [disabled]="isLoading()"
              class="flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
            >
              <ng-icon name="heroArrowPath" class="size-4" [class.animate-spin]="isLoading()" />
              Refresh
            </button>
            <button
              type="button"
              (click)="confirmBulkDelete()"
              [disabled]="selectedCount() === 0 || isDeleting()"
              class="flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm/6 font-medium text-white transition-colors hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-red-500 dark:hover:bg-red-600"
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
                Selection limit reached. You can delete up to {{ maxSelection }} conversations at a time.
              </p>
            </div>
          </div>
        }

        <!-- Loading State -->
        @if (isLoading() && sessions().length === 0) {
          <div class="flex items-center justify-center py-12">
            <div class="text-center">
              <div class="mb-4 inline-block size-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent"></div>
              <p class="text-base/7 text-gray-600 dark:text-gray-400">Loading conversations...</p>
            </div>
          </div>
        } @else if (sessions().length === 0) {
          <!-- Empty State -->
          <div class="rounded-lg border border-gray-200 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
            <ng-icon name="heroChatBubbleLeftRight" class="mx-auto size-12 text-gray-400" />
            <h3 class="mt-4 text-base/7 font-semibold text-gray-900 dark:text-white">No conversations</h3>
            <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
              You don't have any conversations to manage.
            </p>
          </div>
        } @else {
          <!-- Sessions List -->
          <fieldset class="rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <legend class="sr-only">Select conversations to delete</legend>
            <div class="divide-y divide-gray-200 dark:divide-gray-700">
              @for (session of sessions(); track session.sessionId) {
                @let isSelected = selectedSessionIds().has(session.sessionId);
                @let isDisabled = !isSelected && isAtSelectionLimit();
                <div class="relative flex gap-3 px-4 pt-3.5 pb-4">
                  <div class="min-w-0 flex-1">
                    <label
                      [for]="'session-' + session.sessionId"
                      class="block cursor-pointer"
                      [class.cursor-not-allowed]="isDisabled"
                      [class.opacity-50]="isDisabled"
                    >
                      <span class="text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ session.title || 'Untitled Conversation' }}
                      </span>
                      <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ formatDate(session.lastMessageAt) }}
                        @if (session.messageCount) {
                          <span class="mx-1">&middot;</span>
                          {{ session.messageCount }} {{ session.messageCount === 1 ? 'message' : 'messages' }}
                        }
                      </p>
                    </label>
                  </div>
                  <div class="flex h-6 shrink-0 items-center">
                    <div class="group grid size-4 grid-cols-1">
                      <input
                        [id]="'session-' + session.sessionId"
                        type="checkbox"
                        [checked]="isSelected"
                        [disabled]="isDisabled"
                        (change)="toggleSession(session.sessionId)"
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
          @if (hasMoreSessions()) {
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
export class ManageSessionsPage implements OnInit {
  private sessionService = inject(SessionService);
  private toastService = inject(ToastService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  /** Maximum number of sessions that can be selected */
  readonly maxSelection = MAX_SELECTION;

  /** All loaded sessions */
  readonly sessions = signal<SessionMetadata[]>([]);

  /** Set of selected session IDs */
  readonly selectedSessionIds = signal<Set<string>>(new Set());

  /** Loading states */
  readonly isLoading = signal(false);
  readonly isLoadingMore = signal(false);
  readonly isDeleting = signal(false);

  /** Pagination token for loading more */
  private nextToken = signal<string | null>(null);

  /** Number of selected sessions */
  readonly selectedCount = computed(() => this.selectedSessionIds().size);

  /** Whether selection limit is reached */
  readonly isAtSelectionLimit = computed(() => this.selectedCount() >= this.maxSelection);

  /** Whether there are more sessions to load */
  readonly hasMoreSessions = computed(() => this.nextToken() !== null);

  ngOnInit(): void {
    this.loadSessions();
  }

  /**
   * Load sessions from the API
   */
  async loadSessions(): Promise<void> {
    this.isLoading.set(true);

    try {
      const response = await this.sessionService.getSessions({ limit: 50 });
      this.sessions.set(response.sessions);
      this.nextToken.set(response.nextToken);
    } catch (error) {
      console.error('Failed to load sessions:', error);
      this.toastService.error('Failed to load conversations');
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Load more sessions (pagination)
   */
  async loadMore(): Promise<void> {
    const token = this.nextToken();
    if (!token || this.isLoadingMore()) return;

    this.isLoadingMore.set(true);

    try {
      const response = await this.sessionService.getSessions({
        limit: 50,
        next_token: token
      });
      this.sessions.update(current => [...current, ...response.sessions]);
      this.nextToken.set(response.nextToken);
    } catch (error) {
      console.error('Failed to load more sessions:', error);
      this.toastService.error('Failed to load more conversations');
    } finally {
      this.isLoadingMore.set(false);
    }
  }

  /**
   * Refresh the sessions list
   */
  async refresh(): Promise<void> {
    this.clearSelection();
    await this.loadSessions();
  }

  /**
   * Toggle selection of a session
   */
  toggleSession(sessionId: string): void {
    this.selectedSessionIds.update(ids => {
      const newIds = new Set(ids);
      if (newIds.has(sessionId)) {
        newIds.delete(sessionId);
      } else if (newIds.size < this.maxSelection) {
        newIds.add(sessionId);
      }
      return newIds;
    });
  }

  /**
   * Clear all selections
   */
  clearSelection(): void {
    this.selectedSessionIds.set(new Set());
  }

  /**
   * Show confirmation dialog and perform bulk delete
   */
  async confirmBulkDelete(): Promise<void> {
    const count = this.selectedCount();
    if (count === 0) return;

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Delete ${count} Conversation${count === 1 ? '' : 's'}`,
        message: `Are you sure you want to delete ${count} conversation${count === 1 ? '' : 's'}? This action cannot be undone. Your usage data will be preserved for billing purposes.`,
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
    const sessionIds = Array.from(this.selectedSessionIds());
    if (sessionIds.length === 0) return;

    this.isDeleting.set(true);

    try {
      const result = await this.sessionService.bulkDeleteSessions(sessionIds);

      // Remove deleted sessions from the list
      const deletedIds = new Set(
        result.results.filter(r => r.success).map(r => r.sessionId)
      );
      this.sessions.update(sessions =>
        sessions.filter(s => !deletedIds.has(s.sessionId))
      );

      // Clear selection
      this.clearSelection();

      // Show result toast
      if (result.failedCount === 0) {
        this.toastService.success(
          'Conversations Deleted',
          `Successfully deleted ${result.deletedCount} conversation${result.deletedCount === 1 ? '' : 's'}.`
        );
      } else if (result.deletedCount > 0) {
        this.toastService.warning(
          'Partial Deletion',
          `Deleted ${result.deletedCount} conversation${result.deletedCount === 1 ? '' : 's'}, ${result.failedCount} failed.`
        );
      } else {
        this.toastService.error(
          'Deletion Failed',
          `Failed to delete ${result.failedCount} conversation${result.failedCount === 1 ? '' : 's'}.`
        );
      }
    } catch (error) {
      console.error('Bulk delete failed:', error);
      this.toastService.error('Failed to delete conversations');
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
