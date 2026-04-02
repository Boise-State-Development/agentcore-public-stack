import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroClipboard,
  heroArrowUpOnSquare,
  heroCheck,
} from '@ng-icons/heroicons/outline';
import { ShareService, ShareResponse } from '../../services/share/share.service';

export interface ShareModalData {
  sessionId: string;
  ownerEmail: string;
}

type AccessLevel = 'public' | 'specific';

@Component({
  selector: 'app-share-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({ heroXMark, heroClipboard, heroArrowUpOnSquare, heroCheck }),
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
        class="dialog-panel relative w-full transform overflow-hidden rounded-lg bg-white px-4 pt-5 pb-4 text-left shadow-xl sm:my-8 sm:max-w-md sm:p-6 dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        aria-labelledby="share-dialog-title"
      >
        <!-- Header -->
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-2">
            <ng-icon name="heroArrowUpOnSquare" class="size-5 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            <h3 id="share-dialog-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              Share conversation
            </h3>
          </div>
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-primary-500 dark:hover:text-gray-300"
            aria-label="Close dialog"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Access level options -->
        <fieldset class="space-y-2">
          <legend class="sr-only">Access level</legend>

          @for (option of accessOptions; track option.value) {
            <label
              class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors"
              [class]="selectedAccess() === option.value
                ? 'border-primary-500 bg-primary-50 dark:border-primary-400 dark:bg-primary-500/10'
                : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-white/5'"
            >
              <input
                type="radio"
                name="accessLevel"
                [value]="option.value"
                [checked]="selectedAccess() === option.value"
                (change)="selectedAccess.set(option.value)"
                class="mt-0.5 size-4 text-primary-600 focus:ring-primary-500"
              />
              <div>
                <span class="text-sm font-medium text-gray-900 dark:text-white">{{ option.label }}</span>
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ option.description }}</p>
              </div>
            </label>
          }
        </fieldset>

        <!-- Email input (specific access) -->
        @if (selectedAccess() === 'specific') {
          <div class="mt-4">
            <label for="email-input" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              People with access
            </label>

            <!-- Email chips -->
            <div class="flex flex-wrap gap-1.5 mb-2">
              <!-- Owner chip (non-removable) -->
              <span class="inline-flex items-center gap-1 rounded-full bg-primary-100 px-2.5 py-0.5 text-xs font-medium text-primary-700 dark:bg-primary-500/20 dark:text-primary-300">
                {{ data.ownerEmail }} (you)
              </span>

              @for (email of allowedEmails(); track email) {
                <span class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                  {{ email }}
                  <button
                    type="button"
                    (click)="removeEmail(email)"
                    class="ml-0.5 inline-flex size-3.5 items-center justify-center rounded-full hover:bg-gray-200 dark:hover:bg-gray-600"
                    [attr.aria-label]="'Remove ' + email"
                  >
                    <ng-icon name="heroXMark" class="size-3" aria-hidden="true" />
                  </button>
                </span>
              }
            </div>

            <!-- Email input -->
            <div class="flex gap-2">
              <input
                id="email-input"
                type="email"
                placeholder="Enter email address"
                [ngModel]="emailInput()"
                (ngModelChange)="emailInput.set($event)"
                (keydown.enter)="addEmail($event)"
                class="flex-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-primary-400 dark:focus:ring-primary-400"
              />
              <button
                type="button"
                (click)="addEmail()"
                [disabled]="!emailInput().trim()"
                class="rounded-md bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-primary-500 dark:hover:bg-primary-400"
              >
                Add
              </button>
            </div>
          </div>
        }

        <!-- Existing shares info -->
        @if (existingShares().length > 0 && !shareResult()) {
          <div class="mt-4 rounded-md bg-blue-50 p-3 dark:bg-blue-500/10">
            <p class="text-xs text-blue-700 dark:text-blue-300">
              This conversation has {{ existingShares().length }} existing share{{ existingShares().length > 1 ? 's' : '' }}.
              Creating a new share will add another snapshot.
            </p>
          </div>
        }

        <!-- Share result -->
        @if (shareResult()) {
          <div class="mt-4 rounded-md bg-green-50 p-3 dark:bg-green-500/10">
            <p class="text-sm font-medium text-green-800 dark:text-green-300 mb-2">Chat shared</p>
            <p class="text-xs text-green-600 dark:text-green-400 mb-2">Future messages aren't included in the share.</p>
            <div class="flex items-center gap-2">
              <input
                type="text"
                readonly
                [value]="shareUrl()"
                class="flex-1 rounded-md border border-green-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 dark:border-green-700 dark:bg-gray-700 dark:text-gray-300"
                (click)="$event.target"
              />
              <button
                type="button"
                (click)="copyLink()"
                class="inline-flex items-center gap-1 rounded-md bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-gray-700 dark:text-gray-300 dark:ring-gray-600 dark:hover:bg-gray-600"
              >
                <ng-icon [name]="copied() ? 'heroCheck' : 'heroClipboard'" class="size-3.5" aria-hidden="true" />
                {{ copied() ? 'Copied' : 'Copy link' }}
              </button>
            </div>
          </div>
        }

        <!-- Error -->
        @if (error()) {
          <div class="mt-4 rounded-md bg-red-50 p-3 dark:bg-red-500/10">
            <p class="text-sm text-red-700 dark:text-red-300">{{ error() }}</p>
          </div>
        }

        <!-- Actions -->
        <div class="mt-5 flex justify-end gap-3">
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
          >
            {{ shareResult() ? 'Done' : 'Cancel' }}
          </button>

          @if (!shareResult()) {
            <button
              type="button"
              (click)="onShare()"
              [disabled]="isSubmitting() || !canSubmit()"
              class="inline-flex items-center gap-1.5 rounded-md bg-primary-600 px-3 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-primary-500 dark:shadow-none dark:hover:bg-primary-400"
            >
              @if (isSubmitting()) {
                <span class="size-4 animate-spin rounded-full border-2 border-white border-t-transparent" aria-hidden="true"></span>
              }
              Create share link
            </button>
          }
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
export class ShareModalComponent implements OnInit {
  private dialogRef = inject(DialogRef<boolean>);
  protected data = inject<ShareModalData>(DIALOG_DATA);
  private shareService = inject(ShareService);

  // State
  protected selectedAccess = signal<AccessLevel>('public');
  protected allowedEmails = signal<string[]>([]);
  protected emailInput = signal('');
  protected isSubmitting = signal(false);
  protected error = signal<string | null>(null);
  protected shareResult = signal<ShareResponse | null>(null);
  protected existingShares = signal<ShareResponse[]>([]);
  protected copied = signal(false);

  protected readonly accessOptions = [
    { value: 'public' as AccessLevel, label: 'Public link', description: 'Any authenticated user with the link can view' },
    { value: 'specific' as AccessLevel, label: 'Limited share', description: 'Only you and designated email addresses can view' },
  ];

  protected shareUrl = computed(() => {
    const result = this.shareResult();
    if (!result) return '';
    return `${window.location.origin}/shared/${result.shareId}`;
  });

  protected canSubmit = computed(() => {
    // For 'specific' access, owner email is always included automatically,
    // so sharing with just yourself (no additional emails) is valid
    return true;
  });

  async ngOnInit(): Promise<void> {
    try {
      const response = await this.shareService.listSharesForSession(this.data.sessionId);
      this.existingShares.set(response.shares);
    } catch {
      // No existing shares — that's fine
    }
  }

  protected addEmail(event?: Event): void {
    event?.preventDefault();
    const email = this.emailInput().trim().toLowerCase();
    if (!email || !email.includes('@')) return;
    if (email === this.data.ownerEmail.toLowerCase()) return;
    if (this.allowedEmails().includes(email)) return;

    this.allowedEmails.update((list: string[]) => [...list, email]);
    this.emailInput.set('');
  }

  protected removeEmail(email: string): void {
    this.allowedEmails.update((list: string[]) => list.filter((e: string) => e !== email));
  }

  protected async onShare(): Promise<void> {
    this.isSubmitting.set(true);
    this.error.set(null);

    try {
      const emails =
        this.selectedAccess() === 'specific'
          ? [this.data.ownerEmail, ...this.allowedEmails()]
          : undefined;

      const result = await this.shareService.createShare(
        this.data.sessionId,
        this.selectedAccess(),
        emails
      );

      this.shareResult.set(result);
      this.existingShares.update(shares => [...shares, result]);
    } catch (err: unknown) {
      const errorDetail = (err as any)?.error?.detail;
      this.error.set(
        errorDetail || 'Failed to create share. Please try again.'
      );
    } finally {
      this.isSubmitting.set(false);
    }
  }

  protected async copyLink(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.shareUrl());
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    } catch {
      // Fallback: select the input text so the user can copy manually
      const input = document.querySelector<HTMLInputElement>(
        'input[readonly][type="text"]'
      );
      if (input) {
        input.select();
        input.setSelectionRange(0, input.value.length);
      }
      this.error.set('Could not copy automatically. Please copy the selected link manually.');
    }
  }

  protected onClose(): void {
    this.dialogRef.close(!!this.shareResult());
  }
}
