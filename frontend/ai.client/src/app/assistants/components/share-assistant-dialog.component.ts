import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroShare,
  heroLink,
  heroMagnifyingGlass,
  heroUserPlus,
  heroTrash,
  heroPencilSquare,
  heroEye,
  heroChevronDown,
} from '@ng-icons/heroicons/outline';
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
  providers: [
    provideIcons({
      heroXMark,
      heroShare,
      heroLink,
      heroMagnifyingGlass,
      heroUserPlus,
      heroTrash,
      heroPencilSquare,
      heroEye,
      heroChevronDown,
    }),
  ],
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
        class="dialog-panel relative transform overflow-hidden rounded-2xl border border-gray-200 bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        [attr.aria-labelledby]="'dialog-title'"
        [attr.aria-describedby]="'dialog-description'"
        (click)="$event.stopPropagation()"
      >
        <!-- Close button (top-right) -->
        <div class="absolute top-3 right-3 hidden sm:block">
          <button
            type="button"
            (click)="onCancel()"
            class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            aria-label="Close dialog"
          >
            <span class="sr-only">Close</span>
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Header with Icon -->
        <div class="sm:flex sm:items-start">
          <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-2xl bg-blue-100 sm:mx-0 sm:size-10 dark:bg-blue-500/10">
            <ng-icon name="heroShare" class="size-6 text-blue-600 dark:text-blue-400" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3
              id="dialog-title"
              class="text-base/7 font-semibold text-gray-900 dark:text-white"
            >
              Share assistant
            </h3>
            <p
              id="dialog-description"
              class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400"
            >
              {{ data.assistant.name }}
            </p>
          </div>
        </div>

        <!-- Content -->
        <div class="mt-6">
          @if (isPublic()) {
            <!-- Public Assistant: Show shareable URL -->
            <section class="space-y-3">
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">
                This assistant is public and discoverable by everyone. Share this URL to let
                others start a conversation with it.
              </p>
              <div class="flex gap-2">
                <label for="share-url" class="sr-only">Shareable URL</label>
                <input
                  id="share-url"
                  type="text"
                  [value]="shareableUrl()"
                  readonly
                  class="flex-1 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                />
                <button
                  type="button"
                  (click)="copyUrl()"
                  class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                  <span>{{ copied() ? 'Copied!' : 'Copy' }}</span>
                </button>
              </div>
            </section>
          } @else {
            <!-- PRIVATE or SHARED Assistant: shareable URL + add-people + current shares -->
            <div class="space-y-8">
              <!-- Shareable URL section -->
              <section class="space-y-3">
                <div>
                  <label for="share-url-shared" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Shareable URL
                  </label>
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Share this URL with people you've added below. They'll need to be on the list
                    to access it.
                  </p>
                </div>
                <div class="flex gap-2">
                  <input
                    id="share-url-shared"
                    type="text"
                    [value]="shareableUrl()"
                    readonly
                    class="flex-1 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                  <button
                    type="button"
                    (click)="copyUrl()"
                    class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                  >
                    <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                    <span>{{ copied() ? 'Copied!' : 'Copy' }}</span>
                  </button>
                </div>
              </section>

              <!-- Add people section -->
              <section class="space-y-4 border-t border-gray-200 pt-6 dark:border-gray-700">
                <div class="flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <h4 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                      Add people
                    </h4>
                    @if (!isShared()) {
                      <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                        Visibility switches to "Shared" automatically when you add someone.
                      </p>
                    }
                  </div>
                  <div class="flex items-center gap-2">
                    <label for="new-permission-input" class="text-xs/5 font-medium text-gray-600 dark:text-gray-400">
                      Default for new people
                    </label>
                    <!-- appearance-none + overlaid chevron: the native chevron sits at a
                         fixed offset from the right edge regardless of padding, which
                         crowds the rounded-2xl corner. Owning the chevron lets us place
                         it where we want. -->
                    <div class="relative inline-flex">
                      <select
                        id="new-permission-input"
                        [ngModel]="newPermission()"
                        (ngModelChange)="onNewPermissionChange($event)"
                        class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                      >
                        <option value="viewer">Can view & chat</option>
                        <option value="editor">Can edit</option>
                      </select>
                      <ng-icon
                        name="heroChevronDown"
                        class="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                        aria-hidden="true"
                      />
                    </div>
                  </div>
                </div>

                <!-- Mode Toggle (segmented tabs).
                     Active styling rides aria-selected="true" rather than [class.x] bindings —
                     both border-b-blue-600 and border-b-transparent share class-selector
                     specificity, and Tailwind's emit order made the transparent base win.
                     aria-selected:* generates [aria-selected="true"] which has higher
                     specificity and reliably beats the base utility. -->
                <div
                  class="flex gap-1 border-b border-gray-200 dark:border-gray-700"
                  role="tablist"
                  aria-label="How to add people"
                >
                  <button
                    type="button"
                    role="tab"
                    [attr.aria-selected]="searchMode()"
                    (click)="searchMode.set(true)"
                    class="-mb-px inline-flex items-center gap-1.5 border-b-2 border-b-transparent px-3 py-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 aria-selected:border-b-blue-600 aria-selected:font-semibold aria-selected:text-blue-600 dark:text-gray-400 dark:hover:text-white dark:aria-selected:border-b-blue-400 dark:aria-selected:text-blue-400"
                  >
                    <ng-icon name="heroMagnifyingGlass" class="size-4" aria-hidden="true" />
                    Search users
                  </button>
                  <button
                    type="button"
                    role="tab"
                    [attr.aria-selected]="!searchMode()"
                    (click)="searchMode.set(false)"
                    class="-mb-px inline-flex items-center gap-1.5 border-b-2 border-b-transparent px-3 py-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 aria-selected:border-b-blue-600 aria-selected:font-semibold aria-selected:text-blue-600 dark:text-gray-400 dark:hover:text-white dark:aria-selected:border-b-blue-400 dark:aria-selected:text-blue-400"
                  >
                    <ng-icon name="heroUserPlus" class="size-4" aria-hidden="true" />
                    Add by email
                  </button>
                </div>

                <!-- Mode 1: Search Users -->
                @if (searchMode()) {
                  <div class="space-y-3">
                    <div>
                      <label for="search-input" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                        Search for users
                      </label>
                      <input
                        id="search-input"
                        type="text"
                        [ngModel]="searchQuery()"
                        (ngModelChange)="onSearchQueryChange($event)"
                        placeholder="Type a name or email…"
                        class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                      />
                    </div>

                    <!-- Search Results -->
                    @if (searching()) {
                      <!-- Skeleton: three rows matching the real result layout so the
                           container doesn't shift size when results land. -->
                      <ul
                        class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800"
                        aria-hidden="true"
                      >
                        @for (placeholder of skeletonRows; track placeholder) {
                          <li class="flex items-center justify-between px-3 py-2.5 sm:px-4">
                            <div class="flex-1 space-y-1.5">
                              <div class="h-3 w-32 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
                              <div class="h-2.5 w-48 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
                            </div>
                          </li>
                        }
                      </ul>
                      <span class="sr-only" role="status">Searching for users…</span>
                    } @else if (searchResults() && searchResults()!.length > 0) {
                      <ul
                        class="max-h-48 divide-y divide-gray-200 overflow-y-auto overflow-x-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800"
                        role="listbox"
                        aria-label="Search results"
                      >
                        @for (user of searchResults(); track user.userId) {
                          <li>
                            <button
                              type="button"
                              role="option"
                              [attr.aria-selected]="isEmailShared(user.email)"
                              (click)="addUserFromSearch(user)"
                              [disabled]="isEmailShared(user.email)"
                              class="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-60 sm:px-4 dark:hover:bg-gray-700/50"
                            >
                              <div class="min-w-0 flex-1">
                                <div class="truncate font-medium text-gray-900 dark:text-white">{{ user.name }}</div>
                                <div class="truncate text-xs/5 text-gray-500 dark:text-gray-400">{{ user.email }}</div>
                              </div>
                              @if (isEmailShared(user.email)) {
                                <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">Already added</span>
                              }
                            </button>
                          </li>
                        }
                      </ul>
                    } @else if (searchQuery() && searchQuery().length >= 2) {
                      <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                        No users found. Try adding their email manually instead.
                      </p>
                    }
                  </div>
                }

                <!-- Mode 2: Add Email Manually -->
                @if (!searchMode()) {
                  <div class="space-y-3">
                    <div>
                      <label for="email-input" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                        Email addresses
                      </label>
                      <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                        Separate multiple addresses with commas.
                      </p>
                      <textarea
                        id="email-input"
                        [ngModel]="emailInput()"
                        (ngModelChange)="emailInput.set($event)"
                        placeholder="user1@example.com, user2@example.com"
                        rows="3"
                        class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                      ></textarea>
                    </div>
                    <button
                      type="button"
                      (click)="addEmailsFromInput()"
                      [disabled]="!emailInput().trim()"
                      class="inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                    >
                      <ng-icon name="heroUserPlus" class="size-4" aria-hidden="true" />
                      Add emails
                    </button>
                  </div>
                }
              </section>

              <!-- Current Shares section -->
              <section class="space-y-3 border-t border-gray-200 pt-6 dark:border-gray-700">
                <div class="flex items-baseline justify-between">
                  <h4 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                    Currently shared with
                  </h4>
                  @if (!loadingShares()) {
                    <span class="text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">
                      {{ shares().length }}
                    </span>
                  }
                </div>

                @if (loadingShares()) {
                  <!-- Skeleton: single row matching the real layout (email + select + delete)
                       so the container doesn't reflow when shares land. -->
                  <ul
                    class="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"
                    aria-hidden="true"
                  >
                    <li class="flex items-center gap-3 px-3 py-2.5 sm:px-4">
                      <div class="h-3 flex-1 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
                      <div class="h-7 w-20 shrink-0 animate-pulse rounded-2xl bg-gray-200 dark:bg-gray-700"></div>
                      <div class="size-8 shrink-0 animate-pulse rounded-2xl bg-gray-200 dark:bg-gray-700"></div>
                    </li>
                  </ul>
                  <span class="sr-only" role="status">Loading existing shares…</span>
                } @else if (shares().length === 0) {
                  <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-6 text-center dark:border-gray-700 dark:bg-gray-800">
                    <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                      Not shared with anyone yet.
                    </p>
                  </div>
                } @else {
                  <ul
                    class="max-h-56 divide-y divide-gray-200 overflow-y-auto rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800"
                  >
                    @for (entry of shares(); track entry.email) {
                      <li class="flex items-center gap-3 px-3 py-2.5 sm:px-4">
                        <div class="min-w-0 flex-1">
                          <p class="truncate text-sm/6 text-gray-900 dark:text-white">{{ entry.email }}</p>
                        </div>
                        <label class="sr-only" [attr.for]="'perm-' + entry.email">
                          Permission for {{ entry.email }}
                        </label>
                        <div class="relative inline-flex shrink-0">
                          <select
                            [id]="'perm-' + entry.email"
                            [ngModel]="entry.permission"
                            (ngModelChange)="setPermission(entry.email, $event)"
                            class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                          >
                            <option value="viewer">Can view</option>
                            <option value="editor">Can edit</option>
                          </select>
                          <ng-icon
                            name="heroChevronDown"
                            class="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                            aria-hidden="true"
                          />
                        </div>
                        <button
                          type="button"
                          (click)="removeEmail(entry.email)"
                          class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                          [attr.aria-label]="'Remove ' + entry.email"
                        >
                          <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                        </button>
                      </li>
                    }
                  </ul>
                }
              </section>

              @if (error()) {
                <div class="rounded-2xl bg-red-50 px-3 py-2 text-sm/6 text-red-800 dark:bg-red-900/20 dark:text-red-400" role="alert">
                  {{ error() }}
                </div>
              }
            </div>
          }
        </div>

        <!-- Actions -->
        <div class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
          >
            {{ isPublic() ? 'Close' : 'Cancel' }}
          </button>
          @if (!isPublic()) {
            <button
              type="button"
              (click)="onSave()"
              [disabled]="saving()"
              class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              {{ saving() ? 'Saving…' : 'Save changes' }}
            </button>
          }
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

  /** Stable identity array for the skeleton @for loop — keeps trackBy happy without per-render churn. */
  protected readonly skeletonRows = [0, 1, 2];

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
  /** True while loadShares() is in flight on dialog open — drives the shares-list skeleton.
   *  Seeded `true` so the skeleton paints before the constructor's async fetch resolves. */
  protected readonly loadingShares = signal<boolean>(true);
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
    this.loadingShares.set(true);
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
    } finally {
      this.loadingShares.set(false);
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
