import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Dialog, DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroLink,
  heroMagnifyingGlass,
  heroPlus,
  heroTrash,
  heroChevronDown,
  heroLockClosed,
  heroUsers,
  heroGlobeAlt,
  heroBuildingStorefront,
} from '@ng-icons/heroicons/outline';
import { Subject, debounceTime, distinctUntilChanged, switchMap, catchError, of, firstValueFrom } from 'rxjs';
import {
  ShareEntry,
  SharePermission,
  UserPermission,
  UserSearchResult,
} from '../../assistants/models/assistant.model';
import { AssistantService } from '../../assistants/services/assistant.service';
import { UserApiService } from '../../users/services/user-api.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { AgentService } from '../services/agent.service';
import { AgentListingService } from '../services/agent-listing.service';
import { AgentListingBlock } from '../models/agent.model';
import { ListingState } from '../models/store.model';
import { AgentIconComponent } from './agent-icon.component';
import { ListingStatusComponent } from './listing-status.component';
import {
  SubmitListingDialogComponent,
  SubmitListingDialogData,
  SubmitListingDialogResult,
} from './submit-listing-dialog.component';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

/** Good enough to tell an address from a half-typed name — the backend is the authority. */
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * What the dialog needs to know about the agent. Deliberately narrower than `Agent` or
 * `Assistant`: three callers construct this from three different shapes, and a wide type
 * made every one of them cast through `unknown`.
 */
export interface ShareAgentDialogData {
  /** `agentId == assistantId` — one record, two nouns; the share routes use the latter. */
  agent: {
    assistantId: string;
    name: string;
    visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
    userPermission?: UserPermission;
    emoji?: string;
    iconUrl?: string;
  };
}

/** `{ action: 'shared' }` when shares were written; `undefined` if the dialog was dismissed. */
export type ShareAgentDialogResult = { action: 'shared' } | undefined;

/**
 * Share an agent — the whole of "who can reach this?", on one surface.
 *
 * The three ways an agent travels used to live in three unrelated places: a people list
 * and a link here, and marketplace publication on the agent editor. They are one
 * question asked at three widths, so the dialog reads as a ladder of reach:
 *
 *   People with access  →  General access (the link)  →  Marketplace (found without one)
 *
 * **General access is a read-out, not a control.** Visibility in this system is
 * *derived* — adding someone widens PRIVATE to SHARED, publishing widens to PUBLIC — and
 * the agent editor already owns the field. A second control here would be a second
 * source of truth for the one value that decides who can open the agent.
 *
 * ⚠️ The save path must never narrow a PUBLIC agent. Publishing from inside this dialog
 * flips visibility to PUBLIC mid-session, and the share logic derives PRIVATE/SHARED from
 * the list; deriving over a fresh PUBLIC would delist a published agent from under its
 * own listing — the store keeps serving the tile while every visitor 404s on open. Hence
 * {@link visibility} is a signal that publication writes to, and the derivation below
 * refuses to touch PUBLIC at all.
 */
@Component({
  selector: 'app-share-agent-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, FormsModule, NgIcon, AgentIconComponent, ListingStatusComponent],
  providers: [
    provideIcons({
      heroXMark,
      heroLink,
      heroMagnifyingGlass,
      heroPlus,
      heroTrash,
      heroChevronDown,
      heroLockClosed,
      heroUsers,
      heroGlobeAlt,
      heroBuildingStorefront,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative flex max-h-[90vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="share-agent-title"
        aria-describedby="share-agent-description"
      >
        <!-- Header -->
        <div class="flex items-start gap-3 px-6 pt-5">
          <app-agent-icon
            [agentId]="data.agent.assistantId"
            [iconUrl]="data.agent.iconUrl"
            [emoji]="data.agent.emoji"
            [size]="40"
          />
          <div class="min-w-0 flex-1">
            <h2 id="share-agent-title" class="truncate text-lg/7 font-semibold text-gray-900 dark:text-white">
              Share {{ data.agent.name }}
            </h2>
            <p id="share-agent-description" class="mt-0.5 text-sm/6 text-gray-600 dark:text-gray-400">
              Choose who can open this agent, and whether it is listed in the store.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-6 py-5">
          @if (!canManageShares()) {
            <p class="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm/6 text-gray-600 dark:border-gray-700 dark:bg-white/5 dark:text-gray-400">
              Only the owner can change who this agent is shared with. You can still copy
              the link below.
            </p>
          }

          @if (canManageShares()) {
            <!--
              One field, not two modes. This was a "Search users" / "Add by email" tab
              pair, which asked the user to classify what they were about to type before
              typing it. The field resolves that itself: names and partial emails hit the
              directory, and anything that already parses as an address (or a
              comma-separated run of them) can be added outright — so someone who has not
              signed in yet is reachable without switching anything.
            -->
            <section>
              <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Add people</h3>
              <div class="mt-2 flex gap-2">
                <div class="relative min-w-0 flex-1">
                  <ng-icon
                    name="heroMagnifyingGlass"
                    class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                    aria-hidden="true"
                  />
                  <label for="share-add-people" class="sr-only">Add people by name or email</label>
                  <input
                    id="share-add-people"
                    type="text"
                    autocomplete="off"
                    [ngModel]="query()"
                    (ngModelChange)="onQueryChange($event)"
                    (keydown.enter)="addTypedEmails()"
                    placeholder="Name or email address"
                    class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>
                <div class="relative inline-flex shrink-0">
                  <label for="share-new-permission" class="sr-only">Permission for people you add</label>
                  <select
                    id="share-new-permission"
                    [ngModel]="newPermission()"
                    (ngModelChange)="newPermission.set($event)"
                    class="appearance-none rounded-2xl border border-gray-300 bg-white py-2 pl-3 pr-9 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                  >
                    <option value="viewer">Can view</option>
                    <option value="editor">Can edit</option>
                  </select>
                  <ng-icon
                    name="heroChevronDown"
                    class="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                    aria-hidden="true"
                  />
                </div>
              </div>

              @if (query().trim().length > 0) {
                <div class="mt-2 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
                  <!-- Typed addresses come first: someone who pasted a full address is
                       telling us who they mean, and making them wait on a directory
                       round trip to act on it is the tab bar all over again. -->
                  @if (typedEmails().length; as count) {
                    <button
                      type="button"
                      (click)="addTypedEmails()"
                      class="flex w-full items-center gap-2.5 px-4 py-2.5 text-left text-sm/6 text-gray-900 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-primary-500 dark:text-white dark:hover:bg-gray-800"
                    >
                      <ng-icon name="heroPlus" class="size-4 shrink-0 text-primary-accessible dark:text-primary-accessible-dark" aria-hidden="true" />
                      <span class="truncate">
                        Add
                        {{ count === 1 ? typedEmails()[0] : count + ' email addresses' }}
                      </span>
                    </button>
                  }

                  @if (searching()) {
                    <ul aria-hidden="true" class="divide-y divide-gray-200 dark:divide-gray-700">
                      @for (row of skeletonRows; track row) {
                        <li class="space-y-1.5 px-4 py-2.5">
                          <div class="h-3 w-32 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
                          <div class="h-2.5 w-48 animate-pulse rounded bg-gray-100 dark:bg-gray-800"></div>
                        </li>
                      }
                    </ul>
                    <span class="sr-only" role="status">Searching for people…</span>
                  } @else if (searchResults().length) {
                    <ul class="max-h-48 divide-y divide-gray-200 overflow-y-auto dark:divide-gray-700" role="listbox" aria-label="Matching people">
                      @for (user of searchResults(); track user.userId) {
                        <li>
                          <button
                            type="button"
                            role="option"
                            [attr.aria-selected]="isEmailShared(user.email)"
                            [disabled]="isEmailShared(user.email)"
                            (click)="addUserFromSearch(user)"
                            class="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-gray-800"
                          >
                            <span class="min-w-0 flex-1">
                              <span class="block truncate font-medium text-gray-900 dark:text-white">{{ user.name }}</span>
                              <span class="block truncate text-xs/5 text-gray-500 dark:text-gray-400">{{ user.email }}</span>
                            </span>
                            @if (isEmailShared(user.email)) {
                              <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">Already added</span>
                            }
                          </button>
                        </li>
                      }
                    </ul>
                  } @else if (!typedEmails().length && query().trim().length >= 2) {
                    <p class="px-4 py-3 text-sm/6 text-gray-500 dark:text-gray-400">
                      Nobody by that name. Type a full email address to add someone who
                      has not signed in yet.
                    </p>
                  }
                </div>
              }
            </section>
          }

          <!-- People with access -->
          <section [class]="canManageShares() ? 'mt-6' : 'mt-0'">
            <div class="flex items-baseline justify-between gap-3">
              <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">People with access</h3>
              @if (!loadingShares()) {
                <span class="text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ shares().length }}</span>
              }
            </div>

            @if (loadingShares()) {
              <ul aria-hidden="true" class="mt-2 overflow-hidden rounded-2xl border border-gray-200 dark:border-gray-700">
                <li class="flex items-center gap-3 px-4 py-2.5">
                  <div class="h-3 flex-1 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
                  <div class="h-7 w-24 shrink-0 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800"></div>
                </li>
              </ul>
              <span class="sr-only" role="status">Loading who this agent is shared with…</span>
            } @else if (!shares().length) {
              <p class="mt-2 rounded-2xl border border-dashed border-gray-300 px-4 py-5 text-center text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
                Not shared with anyone yet.
              </p>
            } @else {
              <ul class="mt-2 max-h-56 divide-y divide-gray-200 overflow-y-auto rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
                @for (entry of shares(); track entry.email) {
                  <li class="flex items-center gap-3 px-4 py-2.5">
                    <p class="min-w-0 flex-1 truncate text-sm/6 text-gray-900 dark:text-white">{{ entry.email }}</p>
                    @if (canManageShares()) {
                      <div class="relative inline-flex shrink-0">
                        <label class="sr-only" [attr.for]="'share-perm-' + entry.email">
                          Permission for {{ entry.email }}
                        </label>
                        <select
                          [id]="'share-perm-' + entry.email"
                          [ngModel]="entry.permission"
                          (ngModelChange)="setPermission(entry.email, $event)"
                          class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
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
                        [attr.aria-label]="'Remove ' + entry.email"
                        class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:text-gray-500 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                      >
                        <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                      </button>
                    } @else {
                      <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ entry.permission === 'editor' ? 'Can edit' : 'Can view' }}
                      </span>
                    }
                  </li>
                }
              </ul>
            }

            <!--
              Skills v2 §6/D7. Sharing is the grant boundary for everything welded to this
              agent: invited people can invoke its bound skills and search its knowledge
              base whether or not they hold a grant on either. Said next to the list that
              does the granting, not buried in the header.
            -->
            <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
              Anyone with access can use this agent's skills and knowledge.
            </p>
          </section>

          <!-- General access — what the reach is now, and what the link does about it -->
          <section class="mt-6">
            <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">General access</h3>
            <div class="mt-2 flex gap-3 rounded-2xl border border-gray-200 px-4 py-3 dark:border-gray-700">
              <span
                class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-2xl"
                [class]="accessIconClass()"
              >
                <ng-icon [name]="accessIcon()" class="size-4" aria-hidden="true" />
              </span>
              <div class="min-w-0">
                <p class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ accessLabel() }}</p>
                <p class="text-xs/5 text-gray-500 dark:text-gray-400">{{ accessHelp() }}</p>
              </div>
            </div>

            <div class="mt-2 flex gap-2">
              <label for="share-link" class="sr-only">Link to this agent</label>
              <input
                id="share-link"
                type="text"
                readonly
                [value]="shareableUrl()"
                class="min-w-0 flex-1 rounded-2xl border border-gray-300 bg-gray-50 px-3 py-2 text-sm/6 text-gray-600 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300"
              />
              <button
                type="button"
                (click)="copyUrl()"
                class="inline-flex shrink-0 items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                <ng-icon name="heroLink" class="size-4" aria-hidden="true" />
                {{ copied() ? 'Copied' : 'Copy' }}
              </button>
            </div>
          </section>

          <!--
            Marketplace — the widest rung. Owner-only: an editor may change what the agent
            does but not put it on a shelf. Hidden entirely when the routes are unmounted
            so nothing offers a dead click; a listing that already exists keeps the section
            on regardless, because its state is worth reading whether or not new
            submissions are being taken.
          -->
          @if (showMarketplace()) {
            <section class="mt-6">
              <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Marketplace</h3>
              <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">
                A listing is how people find this agent without a link. An admin reviews it
                first, and their decision shows up here.
              </p>

              @if (listingError(); as message) {
                <p role="alert" class="mt-2 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-2.5 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300">
                  {{ message }}
                </p>
              }

              <div class="mt-3 flex gap-3 rounded-2xl border border-gray-200 px-4 py-3 dark:border-gray-700">
                <span class="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-2xl bg-gray-100 text-gray-500 dark:bg-white/5 dark:text-gray-400">
                  <ng-icon name="heroBuildingStorefront" class="size-4" aria-hidden="true" />
                </span>
                <div class="min-w-0 flex-1">
                  @if (listing(); as l) {
                    <app-listing-status [listing]="l" />
                  } @else {
                    <p class="text-sm/6 font-medium text-gray-900 dark:text-white">Not listed</p>
                    <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                      Only people you share it with can find it.
                    </p>
                  }

                  @if (marketplaceAvailable()) {
                    <div class="mt-3 flex flex-wrap items-center gap-2">
                      @if (canSubmit()) {
                        <button
                          type="button"
                          (click)="onSubmitListing()"
                          [disabled]="listingBusy()"
                          class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-xs/5 font-medium text-gray-700 transition hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                        >
                          {{ submitLabel() }}
                        </button>
                      }
                      @if (isInStore()) {
                        <button
                          type="button"
                          (click)="onViewInStore()"
                          class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-xs/5 font-medium text-gray-700 transition hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                        >
                          View in store
                        </button>
                      }
                      @if (canWithdraw()) {
                        <button
                          type="button"
                          (click)="onWithdrawListing()"
                          [disabled]="listingBusy()"
                          class="rounded-2xl px-3 py-1.5 text-xs/5 font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-100"
                        >
                          {{ listingBusy() ? 'Working…' : withdrawLabel() }}
                        </button>
                      }
                    </div>
                  }
                </div>
              </div>
            </section>
          }

          @if (error(); as message) {
            <p role="alert" class="mt-4 rounded-2xl bg-state-danger-50 px-4 py-2.5 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-400">
              {{ message }}
            </p>
          }
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            {{ hasPendingChanges() ? 'Cancel' : 'Done' }}
          </button>
          @if (canManageShares()) {
            <button
              type="button"
              (click)="onSave()"
              [disabled]="saving() || !hasPendingChanges()"
              class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 "
            >
              {{ saving() ? 'Saving…' : 'Save changes' }}
            </button>
          }
        </div>
      </div>
    </div>
  `,
  styles: `
    @reference "../../../styles/theme.css";


    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }

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
  `,
})
export class ShareAgentDialogComponent {
  protected readonly dialogRef = inject<DialogRef<ShareAgentDialogResult>>(DialogRef);
  protected readonly data = inject<ShareAgentDialogData>(DIALOG_DATA);
  private readonly assistantService = inject(AssistantService);
  private readonly userApiService = inject(UserApiService);
  private readonly agentService = inject(AgentService);
  private readonly listingService = inject(AgentListingService);
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);

  /** Stable identities for the skeleton `@for` — no per-render churn. */
  protected readonly skeletonRows = [0, 1, 2];

  // ---- people ----------------------------------------------------------
  protected readonly query = signal('');
  protected readonly searchResults = signal<UserSearchResult[]>([]);
  protected readonly searching = signal(false);
  protected readonly newPermission = signal<SharePermission>('viewer');
  /** Working set for this dialog; diffed against {@link initialShares} on save. */
  protected readonly shares = signal<ShareEntry[]>([]);
  private initialShares: ShareEntry[] = [];
  /** Seeded `true` so the skeleton paints before the constructor's fetch resolves. */
  protected readonly loadingShares = signal(true);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly copied = signal(false);

  /**
   * Live visibility. A signal rather than a read of `data`, because publishing from the
   * marketplace section below widens it to PUBLIC mid-dialog and the save path keys on it.
   */
  private readonly visibility = signal(this.data.agent.visibility);

  /** Sharing is owner-only; an editor may change what the agent does, not who has it. */
  protected readonly canManageShares = computed(
    () => (this.data.agent.userPermission ?? 'owner') === 'owner',
  );

  protected readonly shareableUrl = computed(() => {
    const origin = typeof window !== 'undefined' ? window.location.origin : '';
    return `${origin}?assistantId=${this.data.agent.assistantId}`;
  });

  /**
   * Addresses typed into the field, comma-separated runs included. Empty unless what was
   * typed actually parses — the "Add …" row must never offer to add `jo` as an address.
   */
  protected readonly typedEmails = computed(() => {
    const raw = this.query().trim();
    if (!raw) return [];
    const parts = raw.split(',').map((part) => part.trim().toLowerCase()).filter(Boolean);
    const valid = parts.filter((part) => EMAIL_PATTERN.test(part) && !this.isEmailShared(part));
    // All-or-nothing on a multi-address paste: silently dropping the two malformed
    // addresses out of five reads as "added" while two people never hear about it.
    return valid.length === parts.length ? valid : [];
  });

  /** Nothing to save until the list differs from what was loaded. */
  protected readonly hasPendingChanges = computed(() => {
    const deltas = this.computeDeltas(this.initialShares, this.shares());
    return (
      deltas.adds.length > 0 || deltas.removes.length > 0 || deltas.permissionChanges.length > 0
    );
  });

  // ---- general access (a read-out; the editor owns the field) -----------
  protected readonly accessIcon = computed(() => {
    switch (this.visibility()) {
      case 'PUBLIC':
        return 'heroGlobeAlt';
      case 'SHARED':
        return 'heroUsers';
      default:
        return 'heroLockClosed';
    }
  });

  protected readonly accessIconClass = computed(() =>
    this.visibility() === 'PUBLIC'
      ? 'bg-state-success-50 text-state-success-600 dark:bg-state-success-500/10 dark:text-state-success-400'
      : 'bg-gray-100 text-gray-500 dark:bg-white/5 dark:text-gray-400',
  );

  protected readonly accessLabel = computed(() => {
    switch (this.visibility()) {
      case 'PUBLIC':
        return 'Everyone at Boise State';
      case 'SHARED':
        return 'Only the people listed above';
      default:
        return 'Only you';
    }
  });

  protected readonly accessHelp = computed(() => {
    switch (this.visibility()) {
      case 'PUBLIC':
        return 'Anyone signed in can open this link.';
      case 'SHARED':
        return 'Anyone else who follows the link will not be able to open it.';
      default:
        return 'Add someone above, or list it in the store, to widen this.';
    }
  });

  // ---- marketplace ------------------------------------------------------
  protected readonly listing = signal<AgentListingBlock | undefined>(undefined);
  protected readonly listingBusy = signal(false);
  protected readonly listingError = signal<string | null>(null);
  protected readonly marketplaceAvailable = this.listingService.available;
  /** Prefills the submit dialog's shelf subtitle; read, never written, here. */
  private readonly tagline = signal<string | undefined>(undefined);
  private readonly description = signal<string | undefined>(undefined);

  protected readonly showMarketplace = computed(
    () =>
      this.canManageShares() &&
      (!!this.listing() || this.marketplaceAvailable() === true),
  );

  private listingState(): ListingState | undefined {
    return this.listing()?.state;
  }

  /**
   * Submittable states, mirroring the backend transition table: never submitted,
   * withdrawn (`private`), returned (`changes_requested`), delisted (`taken_down`) — and
   * `published`, which is how an author ships an **update**.
   *
   * `published` belongs here because edits to a published agent land on the draft and
   * reach nobody until a new version is approved: submitting again is the only route to
   * users.
   *
   * The two absences are the two states with a decision already pending: `in_review` has a
   * submission in the queue, and `withdrawal_requested` is waiting on an admin who may yet
   * take the listing down — neither has an edge to `in_review`, so a button here would be a
   * dead click.
   */
  protected readonly canSubmit = computed(() => {
    const state = this.listingState();
    return state !== 'in_review' && state !== 'withdrawal_requested';
  });

  /**
   * True while the thing being submitted (or already submitted) sits on top of a version
   * users can currently see. `changes_requested` counts: an admin who requests changes on
   * a live listing deliberately leaves it published while the author revises.
   */
  private updatesLiveListing(state: ListingState | undefined): boolean {
    return (
      !!this.listing()?.publishedVersion &&
      (state === 'published' || state === 'changes_requested' || state === 'in_review')
    );
  }

  /** A submission sitting on top of a listing users can see — cancellable, not withdrawable. */
  protected readonly hasPendingUpdate = computed(() => {
    const l = this.listing();
    return l?.state === 'in_review' && !!l.submittedFrom && this.updatesLiveListing(l.state);
  });

  protected readonly submitLabel = computed(() => {
    const state = this.listingState();
    if (!state) return 'Submit to marketplace';
    return this.updatesLiveListing(state) ? 'Submit an update' : 'Submit again';
  });

  /**
   * `taken_down` is present now: the machine gained a `taken_down → private` author edge.
   *
   * It was absent because none existed, which left an author with a delisted agent no way to
   * shelve it — and therefore no way to delete it, since delete accepts only `private`. The
   * refusal even told them to "take it back to private first", naming a button this component
   * did not render.
   */
  protected readonly canWithdraw = computed(() => {
    const state = this.listingState();
    return (
      state === 'in_review' ||
      state === 'changes_requested' ||
      state === 'published' ||
      state === 'taken_down'
    );
  });

  protected readonly isTakenDown = computed(() => this.listingState() === 'taken_down');

  protected readonly isPublished = computed(() => this.listingState() === 'published');

  /**
   * In the store *now* — which "View in store" is asking, and which `isPublished` answers
   * wrongly for one state: a pending withdrawal request leaves the listing serving (§5.1).
   */
  protected readonly isInStore = computed(() => {
    const state = this.listingState();
    return state === 'published' || state === 'withdrawal_requested';
  });

  /**
   * "Request withdrawal" on a live listing, not "Unpublish" (§5.1) — the author no longer
   * owns that edge, and "Unpublish" promised an outcome the button cannot deliver.
   *
   * A pending *update* is the third case and needs its own word: the author is taking back
   * an edit, not asking for anything to come down, and the listing goes on serving what it
   * always was. Labelling that "Withdraw submission" would read as though the live listing
   * were at stake.
   */
  protected readonly withdrawLabel = computed(() => {
    if (this.hasPendingUpdate()) return 'Cancel update';
    switch (this.listingState()) {
      case 'published':
        return 'Request withdrawal';
      case 'in_review':
        return 'Withdraw submission';
      // Not "Withdraw" — an admin already took it down, so there is nothing to withdraw
      // from. This shelves the listing, which is also what makes the agent deletable.
      case 'taken_down':
        return 'Remove listing';
      default:
        return 'Withdraw';
    }
  });

  private readonly querySubject = new Subject<string>();

  constructor() {
    void this.loadShares();
    void this.loadListing();

    this.querySubject
      .pipe(
        debounceTime(300),
        distinctUntilChanged(),
        switchMap((query) => {
          if (query.trim().length < 2) {
            this.searchResults.set([]);
            this.searching.set(false);
            return of({ users: [] });
          }
          this.searching.set(true);
          return this.userApiService.searchUsers(query, 20).pipe(
            catchError((err: unknown) => {
              console.error('Error searching users:', err);
              this.error.set('Could not search for people.');
              return of({ users: [] });
            }),
          );
        }),
      )
      .subscribe((response) => {
        this.searchResults.set(response.users ?? []);
        this.searching.set(false);
      });
  }

  // ---- loading ---------------------------------------------------------
  private async loadShares(): Promise<void> {
    this.loadingShares.set(true);
    try {
      // Loaded for every visibility, not just SHARED: an agent that was shared and then
      // published keeps its share records, and the old dialog hid them behind the PUBLIC
      // branch — so the owner could not see, let alone revoke, who else held editor.
      const entries = await this.assistantService.getAssistantShares(this.data.agent.assistantId);
      this.initialShares = entries.map((entry) => ({ ...entry }));
      this.shares.set(entries.map((entry) => ({ ...entry })));
    } catch (err) {
      // A never-shared agent is the common failure here; start empty rather than
      // stranding the dialog on an error it cannot act on.
      console.error('Error loading shares:', err);
      this.initialShares = [];
      this.shares.set([]);
    } finally {
      this.loadingShares.set(false);
    }
  }

  /**
   * The listing block, fetched rather than passed in.
   *
   * Three callers open this dialog from three different records, and only one of them
   * has the listing to hand. Fetching keeps the marketplace section working from all of
   * them instead of appearing only where a caller remembered to thread it through.
   */
  private async loadListing(): Promise<void> {
    if (!this.canManageShares()) return;
    // Doubles as the kill-switch probe — see `AgentListingService.available`.
    void this.listingService.loadCategories();
    try {
      const agent = await this.agentService.getAgent(this.data.agent.assistantId);
      this.listing.set(agent.listing);
      this.tagline.set(agent.tagline);
      this.description.set(agent.description);
      this.visibility.set(agent.visibility);
    } catch (err) {
      // `AGENTS_API_ENABLED=false` 404s this route. Sharing still works; the marketplace
      // section simply stays closed.
      console.error('Error loading agent listing:', err);
    }
  }

  // ---- people ----------------------------------------------------------
  protected onQueryChange(value: string): void {
    this.query.set(value);
    this.querySubject.next(value);
  }

  protected addUserFromSearch(user: UserSearchResult): void {
    this.addEmails([user.email.toLowerCase()]);
  }

  protected addTypedEmails(): void {
    const emails = this.typedEmails();
    if (!emails.length) {
      if (this.query().trim()) {
        this.error.set('That does not look like an email address.');
        setTimeout(() => this.error.set(null), 3000);
      }
      return;
    }
    this.addEmails(emails);
  }

  private addEmails(emails: string[]): void {
    const permission = this.newPermission();
    const fresh = emails.filter((email) => !this.isEmailShared(email));
    if (!fresh.length) return;
    this.shares.update((current) => [...current, ...fresh.map((email) => ({ email, permission }))]);
    this.query.set('');
    this.querySubject.next('');
    this.searchResults.set([]);
  }

  protected setPermission(email: string, permission: SharePermission): void {
    this.shares.update((current) =>
      current.map((entry) => (entry.email === email ? { ...entry, permission } : entry)),
    );
  }

  protected removeEmail(email: string): void {
    this.shares.update((current) => current.filter((entry) => entry.email !== email));
  }

  protected isEmailShared(email: string): boolean {
    const normalized = email.toLowerCase();
    return this.shares().some((entry) => entry.email === normalized);
  }

  // ---- marketplace actions ---------------------------------------------
  protected async onSubmitListing(): Promise<void> {
    this.listingError.set(null);
    const dialogRef = this.dialog.open<SubmitListingDialogResult>(SubmitListingDialogComponent, {
      data: {
        agentId: this.data.agent.assistantId,
        agentName: this.data.agent.name,
        listing: this.listing(),
        tagline: this.tagline(),
        // Only read to prefill an absent tagline (#749).
        description: this.description(),
      } satisfies SubmitListingDialogData,
    });
    const listing = await firstValueFrom(dialogRef.closed);
    if (listing) {
      this.listing.set(listing);
      // Publishing widened visibility in the same write (the dialog's `makePublic`
      // consent). Record it here or the save below derives over a stale PRIVATE and
      // narrows the agent out from under its own live listing.
      this.visibility.set('PUBLIC');
    }
  }

  protected onViewInStore(): void {
    // The dialog is dismissed first: it is an overlay, not part of the route, so leaving
    // it up over the store page it just navigated to would strand the user behind it.
    this.dialogRef.close(undefined);
    void this.router.navigate(['/agents', this.data.agent.assistantId]);
  }

  protected async onWithdrawListing(): Promise<void> {
    const name = this.data.agent.name;
    // A pending update is a different act with a different outcome, and it is neither
    // destructive nor a request: the listing keeps serving its approved version either way.
    // Confirmed anyway, because the edit being taken back is work the author may not have
    // saved anywhere else — they resubmit from the draft, they do not get the snapshot back.
    if (this.hasPendingUpdate()) {
      const cancelRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
        data: {
          title: 'Cancel this update?',
          message:
            `The version of "${name}" in the store now stays live and unchanged — this ` +
            'only takes back the update waiting for review. Your edits stay on the agent, ' +
            'so you can submit again whenever you want.',
          confirmText: 'Cancel update',
          cancelText: 'Keep it in review',
        } as ConfirmationDialogData,
      });
      if (!(await firstValueFrom(cancelRef.closed))) return;
      await this.runWithdraw(`Could not cancel the update to "${name}".`);
      return;
    }

    const published = this.isPublished();
    const takenDown = this.isTakenDown();
    const confirmRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: published
          ? 'Request withdrawal?'
          : takenDown
            ? 'Remove this listing?'
            : 'Withdraw this submission?',
        // Two things this has to get right. (1) §5.1 — this is a *request*: the listing
        // stays in the store until an admin grants it. (2) D7.3 — say plainly that it
        // recalls nothing, because an author who thinks withdrawal revokes access decides
        // worse than one who knows it does not.
        message: published
          ? `An admin reviews this before "${name}" comes down from Discover, so it stays ` +
            'published until they decide. Even once it does come down, nothing is ' +
            'recalled: anyone who already added it keeps it, conversations underway keep ' +
            'running, and it stays reachable by direct link.'
          : takenDown
            ? // Already off the shelf, so this changes nothing users can see. Name the two
              // things the author is actually deciding: they keep the agent either way, and
              // this is what unblocks deleting it if that is where they were heading.
              `"${name}" is already down from Discover, so this only clears its listing. ` +
              'The agent itself is unaffected and stays yours — and once its listing is ' +
              'cleared you can delete it, which a taken-down listing blocks.'
            : `"${name}" is pulled from the review queue. You can submit it again at any time.`,
        confirmText: published ? 'Request withdrawal' : takenDown ? 'Remove listing' : 'Withdraw',
        cancelText: 'Cancel',
        destructive: published,
      } as ConfirmationDialogData,
    });

    if (!(await firstValueFrom(confirmRef.closed))) return;

    await this.runWithdraw(
      published ? `Could not request withdrawal of "${name}".` : `Could not withdraw "${name}".`,
    );
  }

  /**
   * The one call all three acts make (§5.1) — cancel an update, withdraw a submission, or
   * request a delisting. Which one it was is the backend's to decide from the listing, so
   * the SPA sends the same DELETE and reflects whatever state comes back.
   *
   * `fallback` is used only when the error carries no `detail`. The backend's own message
   * names the actual reason, and replacing it with a generic line has bitten this feature
   * before — it told authors to "try again" for something retrying could never fix.
   */
  private async runWithdraw(fallback: string): Promise<void> {
    this.listingBusy.set(true);
    this.listingError.set(null);
    try {
      this.listing.set(await this.listingService.withdraw(this.data.agent.assistantId));
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.listingError.set(typeof detail === 'string' ? detail : fallback);
    } finally {
      this.listingBusy.set(false);
    }
  }

  // ---- save ------------------------------------------------------------
  protected async onSave(): Promise<void> {
    this.saving.set(true);
    this.error.set(null);

    const id = this.data.agent.assistantId;
    try {
      const next = this.shares();
      const current = this.visibility();

      // ⚠️ PUBLIC is left alone. Visibility is derived from the share list only while the
      // agent is not public; deriving over PUBLIC would narrow a published agent to
      // SHARED and leave its store tile serving to people who then cannot open it.
      if (current !== 'PUBLIC') {
        if (current !== 'SHARED' && next.length > 0) {
          await this.assistantService.updateAssistant(id, { visibility: 'SHARED' });
          this.visibility.set('SHARED');
        } else if (current === 'SHARED' && next.length === 0) {
          await this.assistantService.updateAssistant(id, { visibility: 'PRIVATE' });
          this.visibility.set('PRIVATE');
        }
      }

      const deltas = this.computeDeltas(this.initialShares, next);

      // Permission changes are keyed on email, one record at a time.
      for (const change of deltas.permissionChanges) {
        await this.assistantService.updateSharePermission(id, change.email, change.permission);
      }

      // Adds batch by permission — one POST per distinct permission.
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
      this.error.set(err instanceof Error ? err.message : 'Could not save these changes.');
    } finally {
      this.saving.set(false);
    }
  }

  /**
   * Compare loaded vs current shares to decide which calls to make. Adds, removes and
   * permission changes on an already-shared email are three distinct endpoints.
   */
  protected computeDeltas(
    initial: ShareEntry[],
    next: ShareEntry[],
  ): { adds: ShareEntry[]; removes: string[]; permissionChanges: ShareEntry[] } {
    const initialByEmail = new Map(initial.map((entry) => [entry.email, entry.permission]));
    const nextByEmail = new Map(next.map((entry) => [entry.email, entry.permission]));

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
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    navigator.clipboard
      .writeText(this.shareableUrl())
      .then(() => {
        this.copied.set(true);
        setTimeout(() => this.copied.set(false), 2000);
      })
      .catch((err: unknown) => console.error('Error copying the link:', err));
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
