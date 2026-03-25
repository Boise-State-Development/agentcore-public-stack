import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroTrash,
  heroGlobeAlt,
  heroLockClosed,
  heroUserGroup,
  heroChevronDown,
  heroChevronUp,
} from '@ng-icons/heroicons/outline';
import { ShareService, ShareResponse } from '../../session/services/share/share.service';
import { ToastService } from '../../services/toast/toast.service';

export interface ManageSharesDialogData {
  sessionId: string;
  sessionTitle: string;
}

@Component({
  selector: 'app-manage-shares-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroXMark,
      heroTrash,
      heroGlobeAlt,
      heroLockClosed,
      heroUserGroup,
      heroChevronDown,
      heroChevronUp,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'onClose()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
      (click)="onClose()"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative w-full transform overflow-hidden rounded-lg bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:max-w-lg sm:p-6 dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        aria-labelledby="manage-shares-title"
        (click)="$event.stopPropagation()"
      >
        <!-- Header -->
        <div class="flex items-center justify-between mb-4">
          <div>
            <h3 id="manage-shares-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              Manage Shared Instances
            </h3>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400 truncate max-w-sm">
              {{ data.sessionTitle || 'Untitled Conversation' }}
            </p>
          </div>
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-indigo-600 dark:hover:text-gray-300"
            aria-label="Close dialog"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Loading -->
        @if (isLoading()) {
          <div class="flex items-center justify-center py-8">
            <div class="size-6 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-500"></div>
          </div>
        } @else if (shares().length === 0) {
          <!-- Empty state -->
          <div class="py-8 text-center">
            <ng-icon name="heroUserGroup" class="mx-auto size-10 text-gray-400" />
            <p class="mt-3 text-sm/6 text-gray-500 dark:text-gray-400">No shared instances for this conversation.</p>
          </div>
        } @else {
          <!-- Shares list -->
          <div class="max-h-96 overflow-y-auto -mx-4 px-4 sm:-mx-6 sm:px-6 divide-y divide-gray-200 dark:divide-gray-700">
            @for (share of shares(); track share.shareId) {
              <div class="py-3">
                <div class="flex items-center justify-between gap-3">
                  <div class="flex items-center gap-2 min-w-0">
                    @if (share.accessLevel === 'public') {
                      <ng-icon name="heroGlobeAlt" class="size-4 shrink-0 text-green-500" />
                      <span class="text-sm font-medium text-gray-900 dark:text-white">Public</span>
                    } @else {
                      <ng-icon name="heroLockClosed" class="size-4 shrink-0 text-amber-500" />
                      <span class="text-sm font-medium text-gray-900 dark:text-white">Limited Access</span>
                    }
                    <span class="text-xs text-gray-400 dark:text-gray-500">{{ formatDate(share.createdAt) }}</span>
                  </div>
                  <div class="flex items-center gap-2 shrink-0">
                    @if (share.accessLevel === 'specific' && share.allowedEmails?.length) {
                      <button
                        type="button"
                        (click)="toggleExpand(share.shareId)"
                        class="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
                      >
                        {{ share.allowedEmails!.length }} {{ share.allowedEmails!.length === 1 ? 'user' : 'users' }}
                        <ng-icon [name]="expandedShareIds().has(share.shareId) ? 'heroChevronUp' : 'heroChevronDown'" class="size-3.5" />
                      </button>
                    }
                    <button
                      type="button"
                      (click)="revokeShare(share.shareId)"
                      [disabled]="revokingIds().has(share.shareId)"
                      class="rounded-md p-1.5 text-red-500 hover:bg-red-50 disabled:opacity-50 dark:hover:bg-red-500/10"
                      [attr.aria-label]="'Delete share'"
                    >
                      @if (revokingIds().has(share.shareId)) {
                        <div class="size-4 animate-spin rounded-full border-2 border-red-300 border-t-red-600"></div>
                      } @else {
                        <ng-icon name="heroTrash" class="size-4" />
                      }
                    </button>
                  </div>
                </div>

                <!-- Expanded email list for specific shares -->
                @if (share.accessLevel === 'specific' && expandedShareIds().has(share.shareId) && share.allowedEmails?.length) {
                  <div class="mt-2 ml-6 space-y-1">
                    @for (email of share.allowedEmails; track email) {
                      <div class="flex items-center justify-between rounded-md bg-gray-50 px-3 py-1.5 dark:bg-gray-700/50">
                        <span class="text-xs text-gray-700 dark:text-gray-300 truncate">{{ email }}</span>
                        <button
                          type="button"
                          (click)="removeEmailFromShare(share.shareId, email)"
                          [disabled]="removingEmails().has(share.shareId + ':' + email)"
                          class="ml-2 shrink-0 rounded p-0.5 text-red-400 hover:text-red-600 disabled:opacity-50 dark:hover:text-red-300"
                          [attr.aria-label]="'Remove ' + email"
                        >
                          @if (removingEmails().has(share.shareId + ':' + email)) {
                            <div class="size-3.5 animate-spin rounded-full border border-red-300 border-t-red-600"></div>
                          } @else {
                            <ng-icon name="heroXMark" class="size-3.5" />
                          }
                        </button>
                      </div>
                    }
                  </div>
                }
              </div>
            }
          </div>
        }

        <!-- Footer -->
        <div class="mt-5 flex justify-end">
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));

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
      from { opacity: 0; transform: translateY(1rem) scale(0.95); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
  `,
})
export class ManageSharesDialogComponent implements OnInit {
  private dialogRef = inject(DialogRef<boolean>);
  protected data = inject<ManageSharesDialogData>(DIALOG_DATA);
  private shareService = inject(ShareService);
  private toastService = inject(ToastService);

  protected shares = signal<ShareResponse[]>([]);
  protected isLoading = signal(true);
  protected expandedShareIds = signal<Set<string>>(new Set());
  protected revokingIds = signal<Set<string>>(new Set());
  protected removingEmails = signal<Set<string>>(new Set());

  async ngOnInit(): Promise<void> {
    await this.loadShares();
  }

  private async loadShares(): Promise<void> {
    this.isLoading.set(true);
    try {
      const response = await this.shareService.listSharesForSession(this.data.sessionId);
      this.shares.set(response.shares);
    } catch {
      this.toastService.error('Failed to load shared instances');
    } finally {
      this.isLoading.set(false);
    }
  }

  protected toggleExpand(shareId: string): void {
    this.expandedShareIds.update(ids => {
      const next = new Set(ids);
      if (next.has(shareId)) next.delete(shareId);
      else next.add(shareId);
      return next;
    });
  }

  protected async revokeShare(shareId: string): Promise<void> {
    this.revokingIds.update(ids => new Set(ids).add(shareId));
    try {
      await this.shareService.revokeShare(shareId);
      this.shares.update(list => list.filter(s => s.shareId !== shareId));
      this.toastService.success('Share deleted');
    } catch {
      this.toastService.error('Failed to delete share');
    } finally {
      this.revokingIds.update(ids => {
        const next = new Set(ids);
        next.delete(shareId);
        return next;
      });
    }
  }

  protected async removeEmailFromShare(shareId: string, email: string): Promise<void> {
    const key = `${shareId}:${email}`;
    this.removingEmails.update(s => new Set(s).add(key));
    try {
      const share = this.shares().find(s => s.shareId === shareId);
      if (!share || !share.allowedEmails) return;

      const updatedEmails = share.allowedEmails.filter(e => e !== email);

      if (updatedEmails.length === 0) {
        // No emails left, revoke the entire share
        await this.shareService.revokeShare(shareId);
        this.shares.update(list => list.filter(s => s.shareId !== shareId));
        this.toastService.success('Share deleted (no users remaining)');
      } else {
        const updated = await this.shareService.updateShare(shareId, undefined, updatedEmails);
        this.shares.update(list =>
          list.map(s => (s.shareId === shareId ? updated : s))
        );
        this.toastService.success(`Removed ${email}`);
      }
    } catch {
      this.toastService.error('Failed to remove user');
    } finally {
      this.removingEmails.update(s => {
        const next = new Set(s);
        next.delete(key);
        return next;
      });
    }
  }

  protected formatDate(dateString: string): string {
    if (!dateString) return '';
    try {
      const date = new Date(dateString);
      return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    } catch {
      return '';
    }
  }

  protected onClose(): void {
    this.dialogRef.close(true);
  }
}
