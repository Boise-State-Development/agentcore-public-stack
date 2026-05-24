import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroShare, heroLink, heroMagnifyingGlass, heroUserPlus, heroTrash } from '@ng-icons/heroicons/outline';
import { Assistant, ShareEntry, SharePermission, UserSearchResult } from '../models/assistant.model';
import { AssistantService } from '../services/assistant.service';
import { UserApiService } from '../../users/services/user-api.service';
import { Subject, debounceTime, distinctUntilChanged, switchMap, catchError, of } from 'rxjs';

/**
 * Data passed to the share assistant dialog.
 */
export interface ShareAssistantDialogData {
  assistant: Assistant;
}

/**
 * Result returned from the share assistant dialog.
 */
export type ShareAssistantDialogResult = {
  action: 'shared' | 'cancelled';
} | undefined;

/**
 * A dialog for sharing an assistant with specific users or getting a shareable URL.
 * 
 * For PUBLIC assistants: Shows a shareable URL
 * For SHARED assistants: Shows interface to add users via search or manual email input
 */
@Component({
  selector: 'app-share-assistant-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, NgIcon],
  providers: [provideIcons({ heroXMark, heroShare, heroLink, heroMagnifyingGlass, heroUserPlus, heroTrash })],
  host: {
    'class': 'block',
    '(keydown.escape)': 'onCancel()'
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative transform overflow-hidden rounded-lg bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        [attr.aria-labelledby]="'dialog-title'"
        [attr.aria-describedby]="'dialog-description'"
        (click)="$event.stopPropagation()"
      >
        <!-- Close button (top-right) -->
        <div class="absolute top-0 right-0 hidden pt-4 pr-4 sm:block">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-md bg-white text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-indigo-600 dark:bg-gray-800 dark:hover:text-gray-300 dark:focus:outline-white"
            aria-label="Close dialog"
          >
            <span class="sr-only">Close</span>
            <ng-icon name="heroXMark" class="size-6" aria-hidden="true" />
          </button>
        </div>

        <!-- Header with Icon -->
        <div class="sm:flex sm:items-start">
          <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-full bg-indigo-100 sm:mx-0 sm:size-10 dark:bg-indigo-500/10">
            <ng-icon name="heroShare" class="size-6 text-indigo-600 dark:text-indigo-400" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3
              id="dialog-title"
              class="text-base/7 font-semibold text-gray-900 dark:text-white"
            >
              Share Assistant
            </h3>
            <div class="mt-2">
              <p
                id="dialog-description"
                class="text-sm/6 text-gray-500 dark:text-gray-400"
              >
                {{ data.assistant.name }}
              </p>
            </div>
          </div>
        </div>

        <!-- Content -->
        <div class="mt-4">
          @if (isPublic()) {
            <!-- Public Assistant: Show shareable URL -->
            <div class="space-y-3">
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">
                This assistant is public and discoverable by everyone. Share this URL to let others start a conversation with it:
              </p>
              <div class="flex gap-2">
                <input
                  type="text"
                  [value]="shareableUrl()"
                  readonly
                  class="flex-1 rounded-sm border border-gray-300 bg-gray-50 px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:focus:border-blue-400"
                  id="share-url"
                />
                <button
                  type="button"
                  (click)="copyUrl()"
                  class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:hover:bg-gray-600"
                >
                  <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                  <span>{{ copied() ? 'Copied!' : 'Copy' }}</span>
                </button>
              </div>
            </div>
          } @else {
            <!-- PRIVATE or SHARED Assistant: Show shareable URL and user search/email input -->
            <div class="space-y-4">
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">
                Share this assistant with specific users. Only people you share with will be able to access it.
                @if (!isShared()) {
                  <span class="block mt-1 text-xs text-gray-500 dark:text-gray-400">
                    (Visibility will be automatically set to "SHARED" when you add people)
                  </span>
                }
              </p>

              <!-- Shareable URL -->
              <div class="space-y-2">
                <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Shareable URL
                </label>
                <p class="text-xs text-gray-500 dark:text-gray-400">
                  Share this URL with people you've added below. They'll need to be in the share list to access it.
                </p>
                <div class="flex gap-2">
                  <input
                    type="text"
                    [value]="shareableUrl()"
                    readonly
                    class="flex-1 rounded-sm border border-gray-300 bg-gray-50 px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:focus:border-blue-400"
                    id="share-url-shared"
                  />
                  <button
                    type="button"
                    (click)="copyUrl()"
                    class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:hover:bg-gray-600"
                  >
                    <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                    <span>{{ copied() ? 'Copied!' : 'Copy' }}</span>
                  </button>
                </div>
              </div>

              <!-- Divider -->
              <div class="border-t border-gray-200 dark:border-gray-700 pt-4">
                <h4 class="text-sm font-medium text-gray-900 dark:text-white mb-3">
                  Share with specific people
                </h4>
              </div>

              <!-- Mode Toggle -->
              <div class="flex gap-2 border-b border-gray-200 dark:border-gray-700">
                <button
                  type="button"
                  (click)="searchMode.set(true)"
                  [class.border-b-2]="searchMode()"
                  [class.border-indigo-600]="searchMode()"
                  [class.text-indigo-600]="searchMode()"
                  [class.dark:border-indigo-400]="searchMode()"
                  [class.dark:text-indigo-400]="searchMode()"
                  class="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white"
                >
                  <ng-icon name="heroMagnifyingGlass" class="size-4 inline mr-1" />
                  Search Users
                </button>
                <button
                  type="button"
                  (click)="searchMode.set(false)"
                  [class.border-b-2]="!searchMode()"
                  [class.border-indigo-600]="!searchMode()"
                  [class.text-indigo-600]="!searchMode()"
                  [class.dark:border-indigo-400]="!searchMode()"
                  [class.dark:text-indigo-400]="!searchMode()"
                  class="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white"
                >
                  <ng-icon name="heroUserPlus" class="size-4 inline mr-1" />
                  Add Email
                </button>
              </div>

              <!-- Mode 1: Search Users -->
              @if (searchMode()) {
                <div class="space-y-3">
                  <div>
                    <label for="search-input" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Search for users
                    </label>
                    <input
                      id="search-input"
                      type="text"
                      [ngModel]="searchQuery()"
                      (ngModelChange)="onSearchQueryChange($event)"
                      placeholder="Type name or email..."
                      class="w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-blue-400"
                    />
                  </div>

                  <!-- Search Results -->
                  @if (searchResults() && searchResults()!.length > 0) {
                    <div class="max-h-48 overflow-y-auto rounded-sm border border-gray-200 dark:border-gray-700">
                      @for (user of searchResults(); track user.userId) {
                        <button
                          type="button"
                          (click)="addUserFromSearch(user)"
                          [disabled]="isEmailShared(user.email)"
                          class="w-full px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-between"
                        >
                          <div>
                            <div class="font-medium text-gray-900 dark:text-white">{{ user.name }}</div>
                            <div class="text-xs text-gray-500 dark:text-gray-400">{{ user.email }}</div>
                          </div>
                          @if (isEmailShared(user.email)) {
                            <span class="text-xs text-gray-500">Already shared</span>
                          }
                        </button>
                      }
                    </div>
                  } @else if (searchQuery() && searchQuery().length >= 2 && !searching()) {
                    <p class="text-sm text-gray-500 dark:text-gray-400 italic">
                      No users found. Try adding their email manually.
                    </p>
                  }
                </div>
              }

              <!-- Mode 2: Add Email Manually -->
              @if (!searchMode()) {
                <div class="space-y-3">
                  <div>
                    <label for="email-input" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Email addresses (comma-separated)
                    </label>
                    <textarea
                      id="email-input"
                      [ngModel]="emailInput()"
                      (ngModelChange)="emailInput.set($event)"
                      placeholder="user1@example.com, user2@example.com"
                      rows="3"
                      class="w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-blue-400"
                    ></textarea>
                    <button
                      type="button"
                      (click)="addEmailsFromInput()"
                      [disabled]="!emailInput().trim()"
                      class="mt-2 inline-flex items-center gap-2 rounded-sm bg-indigo-600 px-3 py-2 text-sm/6 font-medium text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-indigo-500 dark:hover:bg-indigo-400"
                    >
                      <ng-icon name="heroUserPlus" class="size-4" />
                      Add Emails
                    </button>
                  </div>
                </div>
              }

              <!-- Permission selector for new additions -->
              <div class="flex items-center gap-3 rounded-sm bg-gray-50 px-3 py-2 dark:bg-gray-700/40">
                <label for="new-permission-input" class="text-sm font-medium text-gray-700 dark:text-gray-300">
                  Permission for new people:
                </label>
                <select
                  id="new-permission-input"
                  [ngModel]="newPermission()"
                  (ngModelChange)="onNewPermissionChange($event)"
                  class="rounded-sm border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                >
                  <option value="viewer">Can view & chat</option>
                  <option value="editor">Can edit</option>
                </select>
              </div>

              <!-- Currently Shared List -->
              @if (shares().length > 0) {
                <div class="space-y-2">
                  <h4 class="text-sm font-medium text-gray-900 dark:text-white">Currently shared with:</h4>
                  <div class="space-y-1 max-h-40 overflow-y-auto">
                    @for (entry of shares(); track entry.email) {
                      <div class="flex items-center justify-between gap-2 rounded-sm border border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-700">
                        <span class="text-sm text-gray-900 dark:text-white truncate">{{ entry.email }}</span>
                        <div class="flex items-center gap-2 shrink-0">
                          <label class="sr-only" [attr.for]="'perm-' + entry.email">Permission for {{ entry.email }}</label>
                          <select
                            [id]="'perm-' + entry.email"
                            [ngModel]="entry.permission"
                            (ngModelChange)="setPermission(entry.email, $event)"
                            class="rounded-sm border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                          >
                            <option value="viewer">Can view</option>
                            <option value="editor">Can edit</option>
                          </select>
                          <button
                            type="button"
                            (click)="removeEmail(entry.email)"
                            class="text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                            [attr.aria-label]="'Remove ' + entry.email"
                          >
                            <ng-icon name="heroTrash" class="size-4" />
                          </button>
                        </div>
                      </div>
                    }
                  </div>
                </div>
              }

              @if (error()) {
                <div class="rounded-sm bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-900/20 dark:text-red-400">
                  {{ error() }}
                </div>
              }
            </div>
          }
        </div>

        <!-- Actions -->
        <div class="mt-5 sm:mt-4 sm:flex sm:flex-row-reverse">
          @if (!isPublic()) {
            <button
              type="button"
              (click)="onSave()"
              [disabled]="saving()"
              class="inline-flex w-full justify-center rounded-sm bg-indigo-600 px-3 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed sm:ml-3 sm:w-auto dark:bg-indigo-500 dark:shadow-none dark:hover:bg-indigo-400"
            >
              {{ saving() ? 'Saving...' : 'Save Changes' }}
            </button>
          }
          <button
            type="button"
            (click)="onCancel()"
            class="mt-3 inline-flex w-full justify-center rounded-sm bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 sm:mt-0 sm:w-auto dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
          >
            {{ isPublic() ? 'Close' : 'Cancel' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    /* Backdrop fade-in animation */
    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from {
        opacity: 0;
      } 
      to {
        opacity: 1;
      }
    }

    /* Dialog panel fade-in-up animation */
    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.95);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `
})
export class ShareAssistantDialogComponent {
  protected readonly dialogRef = inject<DialogRef<ShareAssistantDialogResult>>(DialogRef);
  protected readonly data = inject<ShareAssistantDialogData>(DIALOG_DATA);
  protected readonly assistantService = inject(AssistantService);
  protected readonly userApiService = inject(UserApiService);

  protected readonly copied = signal<boolean>(false);
  protected readonly searchMode = signal<boolean>(true); // true = search, false = manual email
  protected readonly searchQuery = signal<string>('');
  protected readonly emailInput = signal<string>('');
  /** Working set of shares for this dialog. Compared against `initialShares` on Save to compute deltas. */
  protected readonly shares = signal<ShareEntry[]>([]);
  /** Snapshot of shares loaded from the API — used to detect adds/removes/permission changes. */
  private initialShares: ShareEntry[] = [];
  /** Permission applied to newly added emails (toggle at top of the add-people section). */
  protected readonly newPermission = signal<SharePermission>('viewer');
  protected readonly searchResults = signal<UserSearchResult[] | null>(null);
  protected readonly searching = signal<boolean>(false);
  protected readonly saving = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);

  protected readonly isPublic = computed<boolean>(() => this.data.assistant.visibility === 'PUBLIC');
  protected readonly isShared = computed<boolean>(() => this.data.assistant.visibility === 'SHARED');
  
  protected readonly shareableUrl = computed<string>(() => {
    const baseUrl = typeof window !== 'undefined' ? window.location.origin : '';
    return `${baseUrl}?assistantId=${this.data.assistant.assistantId}`;
  });

  private searchQuerySubject = new Subject<string>();

  constructor() {
    // Load existing shares
    this.loadShares();

    // Setup debounced search
    this.searchQuerySubject.pipe(
      debounceTime(300),
      distinctUntilChanged(),
      switchMap(query => {
        if (!query || query.length < 2) {
          this.searchResults.set(null);
          return of([]);
        }
        this.searching.set(true);
        return this.userApiService.searchUsers(query, 20).pipe(
          catchError(err => {
            console.error('Search error:', err);
            this.error.set('Failed to search users');
            return of({ users: [] });
          })
        );
      })
    ).subscribe((response: any) => {
      this.searchResults.set(response?.users ?? []);
      this.searching.set(false);
    });
  }

  protected onSearchQueryChange(value: string): void {
    this.searchQuery.set(value);
    this.searchQuerySubject.next(value);
  }

  protected onNewPermissionChange(value: SharePermission): void {
    this.newPermission.set(value);
  }

  protected addUserFromSearch(user: UserSearchResult): void {
    const email = user.email.toLowerCase();
    if (!this.isEmailShared(email)) {
      this.shares.update(current => [...current, { email, permission: this.newPermission() }]);
      this.searchQuery.set('');
      this.searchResults.set(null);
    }
  }

  protected addEmailsFromInput(): void {
    const input = this.emailInput();
    if (!input.trim()) return;

    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const candidates = input
      .split(',')
      .map(e => e.trim().toLowerCase())
      .filter(e => emailRegex.test(e) && !this.isEmailShared(e));

    if (candidates.length > 0) {
      const perm = this.newPermission();
      this.shares.update(current => [
        ...current,
        ...candidates.map(email => ({ email, permission: perm })),
      ]);
      this.emailInput.set('');
    } else {
      this.error.set('Please enter valid email addresses');
      setTimeout(() => this.error.set(null), 3000);
    }
  }

  protected setPermission(email: string, permission: SharePermission): void {
    this.shares.update(current =>
      current.map(entry => (entry.email === email ? { ...entry, permission } : entry)),
    );
  }

  protected removeEmail(email: string): void {
    this.shares.update(current => current.filter(entry => entry.email !== email));
  }

  protected isEmailShared(email: string): boolean {
    const normalized = email.toLowerCase();
    return this.shares().some(entry => entry.email === normalized);
  }

  protected async loadShares(): Promise<void> {
    try {
      // Only try to load shares if assistant is SHARED
      // PRIVATE assistants won't have shares yet
      if (this.isShared()) {
        const entries = await this.assistantService.getAssistantShares(this.data.assistant.assistantId);
        this.initialShares = entries.map(e => ({ ...e }));
        this.shares.set(entries.map(e => ({ ...e })));
      } else {
        this.initialShares = [];
        this.shares.set([]);
      }
    } catch (err) {
      console.error('Failed to load shares:', err);
      // Don't show error for initial load failure - just start with empty list
      this.initialShares = [];
      this.shares.set([]);
    }
  }

  protected async onSave(): Promise<void> {
    this.saving.set(true);
    this.error.set(null);

    try {
      const next = this.shares();
      const isCurrentlyPrivate = !this.isShared();
      const willHaveShares = next.length > 0;

      // If assistant is PRIVATE and we're adding shares, update visibility to SHARED
      if (isCurrentlyPrivate && willHaveShares) {
        await this.assistantService.updateAssistant(this.data.assistant.assistantId, {
          visibility: 'SHARED'
        });
      }
      // If assistant is SHARED and we're removing all shares, update visibility to PRIVATE
      else if (this.isShared() && !willHaveShares) {
        await this.assistantService.updateAssistant(this.data.assistant.assistantId, {
          visibility: 'PRIVATE'
        });
      }

      const deltas = this.computeDeltas(this.initialShares, next);

      // Apply each delta against the backend. The backend handles each grouped batch.
      const id = this.data.assistant.assistantId;

      // Permission changes use PATCH per-email (one record at a time keyed on email)
      for (const change of deltas.permissionChanges) {
        await this.assistantService.updateSharePermission(id, change.email, change.permission);
      }

      // Group adds by permission so we can POST in batches
      const addsByPermission = new Map<SharePermission, string[]>();
      for (const entry of deltas.adds) {
        const bucket = addsByPermission.get(entry.permission) ?? [];
        bucket.push(entry.email);
        addsByPermission.set(entry.permission, bucket);
      }
      for (const [permission, emails] of addsByPermission) {
        await this.assistantService.shareAssistant(id, emails, permission);
      }

      if (deltas.removes.length > 0) {
        await this.assistantService.unshareAssistant(id, deltas.removes);
      }

      this.dialogRef.close({ action: 'shared' });
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to save shares';
      this.error.set(errorMessage);
    } finally {
      this.saving.set(false);
    }
  }

  /**
   * Compare initial vs current shares to determine what API calls are needed.
   * Adds, removes, and permission changes on already-shared emails are all distinct.
   */
  protected computeDeltas(
    initial: ShareEntry[],
    next: ShareEntry[],
  ): { adds: ShareEntry[]; removes: string[]; permissionChanges: ShareEntry[] } {
    const initialByEmail = new Map(initial.map(e => [e.email, e.permission]));
    const nextByEmail = new Map(next.map(e => [e.email, e.permission]));

    const adds: ShareEntry[] = [];
    const removes: string[] = [];
    const permissionChanges: ShareEntry[] = [];

    for (const entry of next) {
      const previous = initialByEmail.get(entry.email);
      if (previous === undefined) {
        adds.push(entry);
      } else if (previous !== entry.permission) {
        permissionChanges.push(entry);
      }
    }

    for (const [email] of initialByEmail) {
      if (!nextByEmail.has(email)) {
        removes.push(email);
      }
    }

    return { adds, removes, permissionChanges };
  }

  protected copyUrl(): void {
    if (typeof navigator === 'undefined' || !navigator.clipboard) {
      return;
    }
    const url = this.shareableUrl();
    navigator.clipboard.writeText(url).then(() => {
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    }).catch(err => {
      console.error('Failed to copy URL:', err);
    });
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
