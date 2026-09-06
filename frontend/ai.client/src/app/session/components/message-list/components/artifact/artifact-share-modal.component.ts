import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroClipboard,
  heroArrowUpOnSquare,
  heroCheck,
  heroGlobeAlt,
  heroLockClosed,
} from '@ng-icons/heroicons/outline';
import {
  ArtifactShareService,
  type ArtifactShare,
  type ArtifactShareAccessLevel,
} from '../../../../services/artifacts/artifact-share.service';
import { DialogDismissDirective } from '../../../../../components/dialog/dialog-dismiss.directive';
import { parseIso } from '../../../../../utils/date';

/** Data passed to the artifact-share dialog. */
export interface ArtifactShareModalData {
  artifactId: string;
  /** The version to share. A share pins one immutable version, never HEAD. */
  version: number;
  title: string;
  ownerEmail: string;
}

/**
 * Result returned when the dialog closes.
 * - `ArtifactShare[]` — the links that exist now, after the user created
 *   and/or revoked at least one. The opener can use it to refresh a
 *   "shared" affordance without a round trip.
 * - `undefined` — nothing was committed (cancelled, Escape, or backdrop).
 */
export type ArtifactShareModalResult = ArtifactShare[] | undefined;

/**
 * Share one artifact version, and manage the links that already exist
 * for this artifact.
 *
 * Adapted from the conversation `ShareModalComponent` — same access-level
 * radio, same email chips, same clipboard copy, same CDK dialog shell —
 * with three deliberate differences:
 *
 *  - **A share pins one immutable version, never HEAD.** Artifact
 *    versions are append-only, so the recipient's view can never change
 *    under them. Existing links are therefore listed *with* their
 *    version: a link made against v1 keeps showing v1 after the model
 *    writes v2.
 *  - **Create and manage live in one dialog.** Conversations split these
 *    across the share modal and the manage-shares dialog because the
 *    latter is reached from the sessions list; an artifact has no
 *    equivalent management surface, so revoke lives here.
 *  - **Redesign tokens, not the source dialog's.** Per the frontend
 *    convention, an adapted dialog copies the older one's *structure*
 *    (backdrop + centred panel + DialogRef wiring) and not its
 *    pre-redesign styling — hence `rounded-2xl` / `text-sm/6` and the
 *    brand `primary-*` / `state-*` tokens throughout.
 *
 * The dialog never touches artifact content. Opening a shared artifact
 * is a separate, access-checked mint on the recipient's side.
 */
@Component({
  selector: 'app-artifact-share-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroXMark,
      heroClipboard,
      heroArrowUpOnSquare,
      heroCheck,
      heroGlobeAlt,
      heroLockClosed,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'onClose()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <!-- Dialog Panel -->
    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onClose()"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="artifact-share-title"
        aria-describedby="artifact-share-description"
      >
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="flex min-w-0 items-start gap-2">
            <ng-icon
              name="heroArrowUpOnSquare"
              class="mt-1 size-5 shrink-0 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <div class="min-w-0">
              <h2
                id="artifact-share-title"
                class="text-lg/7 font-semibold text-gray-900 dark:text-white"
              >
                Share artifact
              </h2>
              <p
                id="artifact-share-description"
                class="mt-1 truncate text-sm/6 text-gray-600 dark:text-gray-400"
              >
                {{ data.title || 'Untitled artifact' }} · version
                {{ data.version }}
              </p>
            </div>
          </div>
          <button
            type="button"
            (click)="onClose()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <!-- Content -->
        <div class="px-6 py-4">
          <!-- Access level -->
          <fieldset class="flex flex-col gap-2">
            <legend class="sr-only">Access level</legend>

            @for (option of accessOptions; track option.value) {
              <label
                class="flex cursor-pointer items-start gap-3 rounded-2xl border p-3 transition-colors"
                [class]="
                  selectedAccess() === option.value
                    ? 'border-primary-500 bg-primary-50 dark:border-primary-400 dark:bg-primary-500/10'
                    : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-700/40'
                "
              >
                <input
                  type="radio"
                  name="artifactAccessLevel"
                  [value]="option.value"
                  [checked]="selectedAccess() === option.value"
                  (change)="selectedAccess.set(option.value)"
                  class="mt-1 size-4 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                />
                <span class="min-w-0">
                  <span
                    class="block text-sm/6 font-medium text-gray-900 dark:text-white"
                    >{{ option.label }}</span
                  >
                  <span
                    class="block text-xs/5 text-gray-500 dark:text-gray-400"
                    >{{ option.description }}</span
                  >
                </span>
              </label>
            }
          </fieldset>

          <!-- Email allowlist (specific access) -->
          @if (selectedAccess() === 'specific') {
            <div class="mt-4">
              <label
                for="artifact-share-email"
                class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300"
              >
                People with access
              </label>

              <div class="mt-1 mb-2 flex flex-wrap gap-1.5">
                <!-- Owner chip: the backend keeps the owner on every
                     allowlist, so it isn't removable here either. -->
                <span
                  class="inline-flex items-center gap-1 rounded-2xl bg-primary-100 px-2.5 py-0.5 text-xs/5 font-medium text-primary-700 dark:bg-primary-500/20 dark:text-primary-300"
                >
                  {{ data.ownerEmail }} (you)
                </span>

                @for (email of allowedEmails(); track email) {
                  <span
                    class="inline-flex items-center gap-1 rounded-2xl bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300"
                  >
                    {{ email }}
                    <button
                      type="button"
                      (click)="removeEmail(email)"
                      class="ml-0.5 inline-flex size-3.5 items-center justify-center rounded-2xl hover:bg-gray-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-600"
                      [attr.aria-label]="'Remove ' + email"
                    >
                      <ng-icon
                        name="heroXMark"
                        class="size-3"
                        aria-hidden="true"
                      />
                    </button>
                  </span>
                }
              </div>

              <div class="flex gap-2">
                <input
                  id="artifact-share-email"
                  type="email"
                  placeholder="Enter email address"
                  [ngModel]="emailInput()"
                  (ngModelChange)="emailInput.set($event)"
                  (keydown.enter)="addEmail($event)"
                  class="flex-1 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                />
                <button
                  type="button"
                  (click)="addEmail()"
                  [disabled]="!emailInput().trim()"
                  class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Add
                </button>
              </div>
            </div>
          }

          <!-- Result of the share just created -->
          @if (shareResult(); as result) {
            <div
              class="mt-4 rounded-2xl border border-state-success-200 bg-state-success-50 p-3 dark:border-state-success-700 dark:bg-state-success-500/10"
            >
              <p
                class="text-sm/6 font-medium text-state-success-800 dark:text-state-success-300"
              >
                Artifact shared
              </p>
              <p class="mb-2 text-xs/5 text-state-success-700 dark:text-state-success-400">
                This link always shows version {{ result.version }}. Later
                versions aren't included.
              </p>
              <div class="flex items-center gap-2">
                <input
                  type="text"
                  readonly
                  [value]="absoluteUrl(result)"
                  aria-label="Share link"
                  class="min-w-0 flex-1 rounded-2xl border border-state-success-200 bg-white px-2.5 py-1.5 text-xs/5 text-gray-700 dark:border-state-success-700 dark:bg-gray-700 dark:text-gray-300"
                />
                <button
                  type="button"
                  (click)="copyLink(result)"
                  class="inline-flex shrink-0 items-center gap-1 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-xs/5 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                >
                  <ng-icon
                    [name]="
                      copiedShareId() === result.shareId
                        ? 'heroCheck'
                        : 'heroClipboard'
                    "
                    class="size-3.5"
                    aria-hidden="true"
                  />
                  {{
                    copiedShareId() === result.shareId ? 'Copied' : 'Copy link'
                  }}
                </button>
              </div>
            </div>
          }

          <!-- Existing links: manage / revoke -->
          @if (otherShares().length > 0) {
            <div class="mt-4">
              <h3
                class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400"
              >
                Existing links
              </h3>
              <ul
                class="mt-1 max-h-48 divide-y divide-gray-200 overflow-y-auto dark:divide-gray-700"
              >
                @for (share of otherShares(); track share.shareId) {
                  <li class="flex items-center justify-between gap-2 py-2">
                    <span class="flex min-w-0 items-center gap-2">
                      @if (share.accessLevel === 'public') {
                        <ng-icon
                          name="heroGlobeAlt"
                          class="size-4 shrink-0 text-state-success-600 dark:text-state-success-400"
                          aria-hidden="true"
                        />
                      } @else {
                        <ng-icon
                          name="heroLockClosed"
                          class="size-4 shrink-0 text-state-warning-600 dark:text-state-warning-400"
                          aria-hidden="true"
                        />
                      }
                      <span
                        class="truncate text-sm/6 text-gray-700 dark:text-gray-300"
                      >
                        <span class="font-medium"
                          >Version {{ share.version }}</span
                        >
                        <span class="text-gray-500 dark:text-gray-400">
                          · {{ audienceLabel(share) }}
                          @if (formatDate(share.createdAt); as d) {
                            · {{ d }}
                          }
                        </span>
                      </span>
                    </span>
                    <span class="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        (click)="copyLink(share)"
                        class="rounded-2xl px-3 py-1 text-xs/5 font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700"
                        [attr.aria-label]="
                          'Copy link for version ' + share.version
                        "
                      >
                        {{
                          copiedShareId() === share.shareId ? 'Copied' : 'Copy'
                        }}
                      </button>
                      <button
                        type="button"
                        (click)="revoke(share)"
                        [disabled]="revokingIds().has(share.shareId)"
                        class="rounded-2xl px-3 py-1 text-xs/5 font-medium text-state-danger-600 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-60 dark:text-state-danger-400 dark:hover:bg-state-danger-500/10"
                        [attr.aria-label]="
                          'Revoke link for version ' + share.version
                        "
                      >
                        {{
                          revokingIds().has(share.shareId)
                            ? 'Revoking…'
                            : 'Revoke'
                        }}
                      </button>
                    </span>
                  </li>
                }
              </ul>
            </div>
          }

          <!-- Error -->
          @if (error()) {
            <p
              class="mt-4 rounded-2xl bg-state-danger-50 p-3 text-sm/6 text-state-danger-700 dark:bg-state-danger-500/10 dark:text-state-danger-300"
              role="alert"
            >
              {{ error() }}
            </p>
          }
        </div>

        <!-- Actions -->
        <div
          class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onClose()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            {{ shareResult() ? 'Done' : 'Cancel' }}
          </button>

          @if (!shareResult()) {
            <button
              type="button"
              (click)="onShare()"
              [disabled]="isSubmitting()"
              class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60"
            >
              @if (isSubmitting()) {
                <span
                  class="size-4 animate-spin rounded-2xl border-2 border-white border-t-transparent"
                  aria-hidden="true"
                ></span>
              }
              Create share link
            </button>
          }
        </div>
      </div>
    </div>
  `,
  styles: `
    @reference "../../../../../../styles/theme.css";

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
        transform: translateY(1rem) scale(0.97);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .dialog-backdrop,
      .dialog-panel {
        animation: none;
      }
    }
  `,
})
export class ArtifactShareModalComponent implements OnInit {
  private dialogRef = inject(DialogRef<ArtifactShareModalResult>);
  protected data = inject<ArtifactShareModalData>(DIALOG_DATA);
  private shareService = inject(ArtifactShareService);

  protected selectedAccess = signal<ArtifactShareAccessLevel>('public');
  protected allowedEmails = signal<string[]>([]);
  protected emailInput = signal('');
  protected isSubmitting = signal(false);
  protected error = signal<string | null>(null);
  protected shareResult = signal<ArtifactShare | null>(null);
  protected shares = signal<ArtifactShare[]>([]);
  protected copiedShareId = signal<string | null>(null);
  protected revokingIds = signal<Set<string>>(new Set());

  /** Set once anything is committed, so closing reports a result rather
   *  than `undefined` ("cancelled") even if the created link was then
   *  revoked in the same dialog. */
  private committed = false;

  protected readonly accessOptions = [
    {
      value: 'public' as ArtifactShareAccessLevel,
      label: 'Public link',
      description: 'Any authenticated user with the link can view',
    },
    {
      value: 'specific' as ArtifactShareAccessLevel,
      label: 'Limited share',
      description: 'Only you and designated email addresses can view',
    },
  ];

  /** Existing links, minus the one just created — that has its own
   *  result panel above and would otherwise be listed twice. */
  protected readonly otherShares = computed(() => {
    const justCreated = this.shareResult()?.shareId;
    return this.shares().filter((s) => s.shareId !== justCreated);
  });

  async ngOnInit(): Promise<void> {
    try {
      this.shares.set(await this.shareService.listShares(this.data.artifactId));
    } catch {
      // No existing links, or the list call failed. Either way the
      // create path still works, so don't block the dialog on it.
    }
  }

  /** The server hands back a SPA-relative route; make it copyable. */
  protected absoluteUrl(share: ArtifactShare): string {
    return `${window.location.origin}${share.shareUrl}`;
  }

  protected audienceLabel(share: ArtifactShare): string {
    if (share.accessLevel === 'public') return 'Anyone signed in';
    const count = share.allowedEmails?.length ?? 0;
    return count === 1 ? '1 person' : `${count} people`;
  }

  protected addEmail(event?: Event): void {
    event?.preventDefault();
    const email = this.emailInput().trim().toLowerCase();
    if (!email || !email.includes('@')) return;
    if (email === this.data.ownerEmail.toLowerCase()) return;
    if (this.allowedEmails().includes(email)) return;

    this.allowedEmails.update((list) => [...list, email]);
    this.emailInput.set('');
  }

  protected removeEmail(email: string): void {
    this.allowedEmails.update((list) => list.filter((e) => e !== email));
  }

  protected async onShare(): Promise<void> {
    this.isSubmitting.set(true);
    this.error.set(null);

    try {
      // The backend keeps the owner on the allowlist itself, but sending
      // it keeps the request self-describing and satisfies the
      // "specific needs at least one email" validator when the user
      // shares with nobody but themselves.
      const emails =
        this.selectedAccess() === 'specific'
          ? [this.data.ownerEmail, ...this.allowedEmails()]
          : undefined;

      const result = await this.shareService.createShare(
        this.data.artifactId,
        this.data.version,
        this.selectedAccess(),
        emails,
      );

      this.committed = true;
      this.shareResult.set(result);
      this.shares.update((list) => [...list, result]);
    } catch (err: unknown) {
      this.error.set(
        describeShareError(err) ?? 'Failed to create share. Please try again.',
      );
    } finally {
      this.isSubmitting.set(false);
    }
  }

  protected async revoke(share: ArtifactShare): Promise<void> {
    if (this.revokingIds().has(share.shareId)) return;
    this.revokingIds.update((ids) => new Set(ids).add(share.shareId));
    this.error.set(null);
    try {
      await this.shareService.revokeShare(share.shareId);
      this.committed = true;
      this.shares.update((list) =>
        list.filter((s) => s.shareId !== share.shareId),
      );
      // Revoking the link that was just created retires its result panel
      // too, so the dialog can't offer a dead link to copy.
      if (this.shareResult()?.shareId === share.shareId) {
        this.shareResult.set(null);
      }
    } catch (err: unknown) {
      this.error.set(describeShareError(err) ?? 'Failed to revoke this link.');
    } finally {
      this.revokingIds.update((ids) => {
        const next = new Set(ids);
        next.delete(share.shareId);
        return next;
      });
    }
  }

  protected async copyLink(share: ArtifactShare): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.absoluteUrl(share));
      this.copiedShareId.set(share.shareId);
      setTimeout(() => {
        if (this.copiedShareId() === share.shareId) {
          this.copiedShareId.set(null);
        }
      }, 2000);
    } catch {
      this.error.set(
        'Could not copy automatically. Please copy the link manually.',
      );
    }
  }

  protected formatDate(value: string): string {
    if (!value) return '';
    try {
      return parseIso(value).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return '';
    }
  }

  /**
   * Escape, backdrop click, Cancel and Done all converge here.
   *
   * Creating and revoking commit immediately, so "cancelled" can only
   * mean *nothing was committed* — that is what returns `undefined`.
   * Once anything has been committed the dialog resolves with the links
   * that exist now.
   */
  protected onClose(): void {
    this.dialogRef.close(this.committed ? this.shares() : undefined);
  }
}

/** Map a backend failure to something a person can act on. The share
 *  routes 404 an unknown share/version, 403 a non-owner, and 503 when
 *  the backing store is briefly unavailable. */
function describeShareError(err: unknown): string | null {
  const status = (err as { status?: number } | null)?.status;
  const detail = (err as { error?: { detail?: string } } | null)?.error?.detail;

  if (status === 404) return 'That artifact version no longer exists.';
  if (status === 403) return 'You do not have permission to change this share.';
  if (status === 503) {
    return 'Sharing is temporarily unavailable. Please try again.';
  }
  return detail ?? null;
}
