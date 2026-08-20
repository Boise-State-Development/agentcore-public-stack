import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroShare,
  heroUserPlus,
  heroTrash,
  heroChevronDown,
} from '@ng-icons/heroicons/outline';
import { MemorySpaceSummary, ShareRole, SpaceMember } from '../models/memory-space.model';
import { MemorySpaceService } from '../services/memory-space.service';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

export interface ShareSpaceDialogData {
  space: MemorySpaceSummary;
}

export type ShareSpaceDialogResult = { action: 'shared' } | undefined;

/** A working grant row (mirrors SpaceMember minus the server timestamp). */
interface GrantRow {
  email: string;
  permission: ShareRole;
}

/**
 * Share a Memory Space with other users by email (owner only). Mirrors the
 * assistant share dialog's add-by-email + current-shares + delta-on-save
 * ergonomics, over the `/memory/spaces/{id}/shares` endpoints. No visibility
 * concept — a grant simply exists (identity-based access, no content gate).
 */
@Component({
  selector: 'app-share-space-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, FormsModule, NgIcon],
  providers: [provideIcons({ heroXMark, heroShare, heroUserPlus, heroTrash, heroChevronDown })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative transform overflow-hidden rounded-2xl border border-gray-200 bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:w-full sm:max-w-lg sm:p-6 dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="share-space-title"
        (click)="$event.stopPropagation()"
      >
        <div class="absolute top-3 right-3 hidden sm:block">
          <button
            type="button"
            (click)="onCancel()"
            class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            aria-label="Close dialog"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="sm:flex sm:items-start">
          <div class="mx-auto flex size-12 shrink-0 items-center justify-center rounded-2xl bg-primary-100 sm:mx-0 sm:size-10 dark:bg-primary-500/10">
            <ng-icon name="heroShare" class="size-6 text-primary-accessible dark:text-primary-accessible-dark" aria-hidden="true" />
          </div>
          <div class="mt-3 text-center sm:mt-0 sm:ml-4 sm:text-left">
            <h3 id="share-space-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              Share memory space
            </h3>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">{{ data.space.name }}</p>
          </div>
        </div>

        <div class="mt-6 space-y-8">
          <!-- Add people -->
          <section class="space-y-3">
            <div>
              <label for="member-email" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Add people by email
              </label>
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                Sharing includes current and future entries, including ones the agent writes later.
              </p>
            </div>
            <div class="flex flex-col gap-2 sm:flex-row">
              <input
                id="member-email"
                type="email"
                [ngModel]="emailInput()"
                (ngModelChange)="emailInput.set($event)"
                (keydown.enter)="addEmail()"
                placeholder="person@example.edu"
                class="flex-1 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              />
              <div class="relative inline-flex">
                <label for="new-permission" class="sr-only">Permission for new people</label>
                <select
                  id="new-permission"
                  [ngModel]="newPermission()"
                  (ngModelChange)="newPermission.set($event)"
                  class="h-full appearance-none rounded-2xl border border-gray-300 bg-white py-2 pl-3 pr-9 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                >
                  <option value="viewer">Can view</option>
                  <option value="editor">Can edit</option>
                </select>
                <ng-icon
                  name="heroChevronDown"
                  class="pointer-events-none absolute right-3 top-1/2 size-3.5 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                  aria-hidden="true"
                />
              </div>
              <button
                type="button"
                (click)="addEmail()"
                [disabled]="!emailInput().trim()"
                class="inline-flex items-center justify-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <ng-icon name="heroUserPlus" class="size-4" aria-hidden="true" />
                Add
              </button>
            </div>
          </section>

          <!-- Current shares -->
          <section class="space-y-3 border-t border-gray-200 pt-6 dark:border-gray-700">
            <div class="flex items-baseline justify-between">
              <h4 class="text-base/7 font-semibold text-gray-900 dark:text-white">Shared with</h4>
              @if (!loading()) {
                <span class="text-xs/5 tabular-nums text-gray-500 dark:text-gray-400">{{ grants().length }}</span>
              }
            </div>

            @if (loading()) {
              <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
                <div class="h-3 w-40 animate-pulse rounded bg-gray-200 dark:bg-gray-700"></div>
              </div>
            } @else if (grants().length === 0) {
              <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-6 text-center dark:border-gray-700 dark:bg-gray-800">
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">Not shared with anyone yet.</p>
              </div>
            } @else {
              <ul class="max-h-56 divide-y divide-gray-200 overflow-y-auto rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
                @for (grant of grants(); track grant.email) {
                  <li class="flex items-center gap-3 px-3 py-2.5 sm:px-4">
                    <p class="min-w-0 flex-1 truncate text-sm/6 text-gray-900 dark:text-white">{{ grant.email }}</p>
                    <div class="relative inline-flex shrink-0">
                      <label class="sr-only" [attr.for]="'perm-' + grant.email">Permission for {{ grant.email }}</label>
                      <select
                        [id]="'perm-' + grant.email"
                        [ngModel]="grant.permission"
                        (ngModelChange)="setPermission(grant.email, $event)"
                        class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pl-2.5 pr-8 text-xs/5 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
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
                      (click)="removeEmail(grant.email)"
                      class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:text-gray-500 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                      [attr.aria-label]="'Remove ' + grant.email"
                    >
                      <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                    </button>
                  </li>
                }
              </ul>
            }

            @if (error()) {
              <div class="rounded-2xl bg-state-danger-50 px-3 py-2 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-400" role="alert">
                {{ error() }}
              </div>
            }
          </section>
        </div>

        <div class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="onSave()"
            [disabled]="saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save changes' }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));
    .dialog-backdrop { animation: backdrop-fade-in 200ms ease-out; }
    @keyframes backdrop-fade-in { from { opacity: 0; } to { opacity: 1; } }
    .dialog-panel { animation: dialog-fade-in-up 200ms ease-out; }
    @keyframes dialog-fade-in-up {
      from { opacity: 0; transform: translateY(1rem) scale(0.95); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
  `,
})
export class ShareSpaceDialogComponent {
  protected readonly dialogRef = inject<DialogRef<ShareSpaceDialogResult>>(DialogRef);
  protected readonly data = inject<ShareSpaceDialogData>(DIALOG_DATA);
  private readonly service = inject(MemorySpaceService);

  protected readonly emailInput = signal<string>('');
  protected readonly newPermission = signal<ShareRole>('viewer');
  protected readonly grants = signal<GrantRow[]>([]);
  protected readonly loading = signal<boolean>(true);
  protected readonly saving = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);

  /** Snapshot loaded from the API — diffed on save to compute the grant deltas. */
  private initialGrants: GrantRow[] = [];

  private static readonly EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  constructor() {
    void this.loadShares();
  }

  protected async loadShares(): Promise<void> {
    this.loading.set(true);
    try {
      const response = await this.service.listShares(this.data.space.spaceId);
      const rows = (response?.members ?? []).map((m: SpaceMember) => ({
        email: m.email,
        permission: m.permission,
      }));
      this.initialGrants = rows.map((r) => ({ ...r }));
      this.grants.set(rows);
    } catch {
      // A viewer/editor without owner rights can't list — start empty and let
      // Save surface any 403. Initial-load failure is not a user-facing error.
      this.initialGrants = [];
      this.grants.set([]);
    } finally {
      this.loading.set(false);
    }
  }

  protected addEmail(): void {
    const email = this.emailInput().trim().toLowerCase();
    if (!email) {
      return;
    }
    if (!ShareSpaceDialogComponent.EMAIL_RE.test(email)) {
      this.flashError('Please enter a valid email address');
      return;
    }
    if (this.grants().some((g) => g.email === email)) {
      this.flashError('That person already has access');
      return;
    }
    this.grants.update((current) => [...current, { email, permission: this.newPermission() }]);
    this.emailInput.set('');
  }

  protected setPermission(email: string, permission: ShareRole): void {
    this.grants.update((current) =>
      current.map((g) => (g.email === email ? { ...g, permission } : g)),
    );
  }

  protected removeEmail(email: string): void {
    this.grants.update((current) => current.filter((g) => g.email !== email));
  }

  protected async onSave(): Promise<void> {
    this.saving.set(true);
    this.error.set(null);
    const spaceId = this.data.space.spaceId;

    try {
      const initial = new Map(this.initialGrants.map((g) => [g.email, g.permission]));
      const next = new Map(this.grants().map((g) => [g.email, g.permission]));

      for (const [email, permission] of next) {
        const previous = initial.get(email);
        if (previous === undefined) {
          await this.service.addShare(spaceId, { email, permission });
        } else if (previous !== permission) {
          await this.service.updateShare(spaceId, email, permission);
        }
      }
      for (const [email] of initial) {
        if (!next.has(email)) {
          await this.service.removeShare(spaceId, email);
        }
      }
      this.dialogRef.close({ action: 'shared' });
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to save shares');
    } finally {
      this.saving.set(false);
    }
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }

  private flashError(message: string): void {
    this.error.set(message);
    setTimeout(() => this.error.set(null), 3000);
  }
}
