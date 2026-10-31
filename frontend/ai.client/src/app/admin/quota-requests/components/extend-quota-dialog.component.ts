import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { RouterLink } from '@angular/router';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { UserSearchResult } from '../../../assistants/models/assistant.model';
import {
  endOfPeriod,
  formatAmount,
  isoDate,
  nextOccurrence,
  perPeriod,
} from '../../../services/quota/quota-format';
import {
  AdminQuotaRequest,
  AdminQuotaRequestPolicy,
  ApproveResponse,
  FundingMode,
  FundingSource,
  QuotaExtension,
  toAmount,
} from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';
import {
  cleanFundingValues,
  describeFundingSource,
  fundingFieldError,
  standingConfirmation,
} from '../utils/quota-admin-copy';
import { FundingSourceFieldsComponent } from './funding-source-fields.component';
import { QuotaUserPickerComponent } from './quota-user-picker.component';

/** Matches `ADMIN_AMOUNT_CEILING` in `apis/shared/quota_requests/models.py`. */
const ADMIN_AMOUNT_CEILING = 10_000;

export type ExtendQuotaDialogData =
  | {
      mode: 'approve';
      request: AdminQuotaRequest;
      policy: AdminQuotaRequestPolicy;
      /** The requester's name or email, for the standing confirmation. */
      userLabel: string;
    }
  | {
      mode: 'direct';
      policy: AdminQuotaRequestPolicy;
      /** Pre-picked and fixed when opened from that user's page. */
      user?: UserSearchResult | null;
    };

/**
 * What happened, or undefined if cancelled. `askForFunding` means the admin
 * chose to send the request back for a funding source instead of approving.
 */
export type ExtendQuotaDialogResult =
  | { kind: 'approved'; response: ApproveResponse }
  | { kind: 'created'; extension: QuotaExtension }
  | { kind: 'askForFunding' }
  | undefined;

type Kind = 'one_time' | 'standing';

/**
 * The approve form and the direct *Extend quota* form (spec §6.3, §6.4).
 *
 * One form because they grant the same thing: an extension with an amount, an
 * end date and a funding mode. The direct form adds a user picker and a
 * required reason. Both offer the Kind switch; *Standing chargeback* forces
 * charge-back, requires the fund, defaults the end to the fiscal year end and
 * the amount to the policy's cap, and makes *No cap* an explicit tick with a
 * warning. Creating one asks for a confirmation sentence that names the user,
 * the fund and the cap, because it commits someone else's money.
 */
@Component({
  selector: 'app-extend-quota-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, FundingSourceFieldsComponent, QuotaUserPickerComponent, RouterLink],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="title()" [description]="description()" size="lg" (closed)="cancel()">
      @if (confirming()) {
        <div class="rounded-2xl border border-state-warning-300 bg-state-warning-50 p-4 dark:border-state-warning-700 dark:bg-state-warning-900/20">
          <p class="text-sm/6 font-medium text-gray-900 dark:text-white" data-testid="standing-confirmation">{{ confirmationSentence() }}</p>
          <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">They are notified, and their Usage page shows these terms right away.</p>
        </div>
      } @else {
        <div class="space-y-5">
          @if (data.mode === 'direct') {
            <app-quota-user-picker label="User" [locked]="!!data.user" [(selected)]="user" />
          }

          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">Kind</legend>
            <div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
              <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                <input type="radio" name="extend-kind" value="one_time" [checked]="kind() === 'one_time'" (change)="setKind('one_time')"
                  class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                One-time extension
              </label>
              <label class="flex items-center gap-2 text-sm/6" [class]="standingAvailable() ? 'text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400'">
                <input type="radio" name="extend-kind" value="standing" [checked]="kind() === 'standing'" (change)="setKind('standing')"
                  [disabled]="!standingAvailable()" [attr.aria-describedby]="standingAvailable() ? null : 'standing-unavailable'"
                  class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible disabled:opacity-50" />
                Standing chargeback
              </label>
            </div>
            @if (!standingAvailable()) {
              <p id="standing-unavailable" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                Standing chargeback needs funding fields and standing extensions turned on in
                <a routerLink="/admin/quota-requests/settings" (click)="cancel()" class="font-medium text-primary-accessible underline dark:text-primary-accessible-dark">Settings</a>.
              </p>
            }
          </fieldset>

          <div>
            <label for="extend-amount" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
              {{ kind() === 'standing' ? 'Cap ' + per() : 'Amount ' + per() }}
            </label>
            <div class="mt-1 flex flex-wrap items-center gap-3">
              <div class="relative w-40">
                <span class="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm/6 text-gray-500 dark:text-gray-400">$</span>
                <input
                  id="extend-amount"
                  type="number"
                  inputmode="decimal"
                  min="0.01"
                  step="0.01"
                  cdkFocusInitial
                  [value]="amountText()"
                  (input)="amountText.set(inputValue($event))"
                  [disabled]="noCap()"
                  [attr.aria-invalid]="amountError() ? 'true' : null"
                  [attr.aria-describedby]="amountError() ? 'extend-amount-error' : null"
                  class="block w-full rounded-2xl border border-gray-300 bg-white py-1.5 pl-7 pr-3 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                />
              </div>
              @if (kind() === 'standing') {
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="checkbox" [checked]="noCap()" (change)="noCap.set(checked($event))"
                    class="size-4 rounded-sm border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  No cap
                </label>
              }
            </div>
            @if (amountError(); as message) {
              <p id="extend-amount-error" class="mt-1 text-xs/5 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
            }
            @if (kind() === 'standing' && noCap()) {
              <p role="alert" class="mt-2 rounded-2xl bg-state-warning-50 px-3 py-2 text-sm/6 text-state-warning-900 dark:bg-state-warning-900/20 dark:text-state-warning-200">
                ⚠ Without a cap one runaway conversation can bill this fund without limit.
              </p>
            }
            @if (data.mode === 'approve' && kind() === 'one_time') {
              <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">They asked for {{ requestedAmountLabel() }}. You can grant more or less.</p>
            }
          </div>

          <div>
            <label for="extend-through" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Through</label>
            <input
              id="extend-through"
              type="date"
              [min]="today"
              [value]="through()"
              (input)="through.set(inputValue($event))"
              [attr.aria-describedby]="kind() === 'standing' ? 'extend-through-help' : null"
              class="mt-1 block w-48 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
            />
            @if (kind() === 'standing') {
              <p id="extend-through-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Defaults to the fiscal year end ({{ data.policy.fiscalYearEnd }}).</p>
            }
          </div>

          @if (kind() === 'one_time') {
            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">Funding</legend>
              <div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="radio" name="extend-funding" [checked]="fundingMode() === 'central'" (change)="fundingMode.set('central')"
                    class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  Centrally funded
                </label>
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="radio" name="extend-funding" [checked]="fundingMode() === 'chargeback'" (change)="fundingMode.set('chargeback')"
                    class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  Charge to a funding source
                </label>
              </div>
            </fieldset>
          }

          @if (chargeback()) {
            <div>
              <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
                Funding source @if (kind() === 'standing') { <span class="font-normal text-gray-600 dark:text-gray-400">(required)</span> }
              </p>
              <app-funding-source-fields
                class="mt-2"
                [fields]="data.policy.fundingFields"
                [showErrors]="attempted()"
                [(values)]="fundingValues"
                [(label)]="fundingLabel"
                [(authorizerEmail)]="authorizerEmail"
              />
              @if (canAskForFunding() && !fundingComplete()) {
                <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">
                  Don't know the fund?
                  <button type="button" (click)="askForFunding()"
                    class="font-medium text-primary-accessible underline hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark">
                    Ask the user for a funding source
                  </button>
                </p>
              }
            </div>
          }

          @if (data.mode === 'direct') {
            <div>
              <label for="extend-reason" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Reason</label>
              <p class="text-xs/5 text-gray-600 dark:text-gray-400">For the audit log and the Extensions tab.</p>
              <textarea id="extend-reason" rows="2" [value]="reason()" (input)="reason.set(inputValue($event))"
                [attr.aria-invalid]="attempted() && !reason().trim() ? 'true' : null"
                class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"></textarea>
            </div>
          }

          <div>
            <label for="extend-note" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Note to user (optional)</label>
            <textarea id="extend-note" rows="2" [value]="note()" (input)="note.set(inputValue($event))"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"></textarea>
          </div>
        </div>
      }

      @if (error(); as message) {
        <p role="alert" class="mt-4 text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
      }

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="confirming() ? confirming.set(false) : cancel()"
          class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          {{ confirming() ? 'Back' : 'Cancel' }}
        </button>
        <button
          type="button"
          [disabled]="saving() || (attempted() && !valid())"
          (click)="submit()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {{ saving() ? 'Saving…' : confirming() ? 'Confirm' : submitLabel() }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class ExtendQuotaDialogComponent {
  private dialogRef = inject<DialogRef<ExtendQuotaDialogResult>>(DialogRef);
  private service = inject(QuotaRequestsAdminService);
  readonly data = inject<ExtendQuotaDialogData>(DIALOG_DATA);

  readonly today = isoDate(new Date());

  readonly standingAvailable = computed(
    () => this.data.policy.standingEnabled && this.data.policy.fundingFields.length > 0,
  );

  readonly user = signal<UserSearchResult | null>(this.data.mode === 'direct' ? (this.data.user ?? null) : null);
  readonly kind = signal<Kind>(
    this.data.mode === 'approve' && this.data.request.kind === 'standing' && this.standingAvailable() ? 'standing' : 'one_time',
  );
  readonly amountText = signal('');
  readonly noCap = signal(false);
  readonly through = signal('');
  readonly fundingMode = signal<FundingMode>(this.data.policy.defaultFundingMode ?? 'central');
  readonly fundingValues = signal<Record<string, string>>({});
  readonly fundingLabel = signal('');
  readonly authorizerEmail = signal('');
  readonly reason = signal('');
  readonly note = signal('');

  readonly attempted = signal(false);
  readonly confirming = signal(false);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);

  readonly per = computed(() => perPeriod(this.data.mode === 'approve' ? this.data.request.context.periodType : 'monthly'));
  readonly chargeback = computed(() => this.kind() === 'standing' || this.fundingMode() === 'chargeback');

  readonly title = computed(() => (this.data.mode === 'approve' ? 'Approve request' : 'Extend quota'));
  readonly description = computed(() =>
    this.data.mode === 'approve'
      ? `Grant ${this.data.userLabel} more quota. They are notified with your note.`
      : 'Add quota for one person without a request. They are notified.',
  );
  readonly submitLabel = computed(() => {
    if (this.kind() === 'standing') return this.data.mode === 'approve' ? 'Approve as standing chargeback' : 'Create standing chargeback';
    return this.data.mode === 'approve' ? 'Approve' : 'Extend quota';
  });

  readonly requestedAmountLabel = computed(() =>
    this.data.mode === 'approve' ? `${formatAmount(toAmount(this.data.request.requestedAmount))} ${this.per()}` : '',
  );

  readonly amount = computed<number | null>(() => {
    if (this.kind() === 'standing' && this.noCap()) return null;
    return toAmount(this.amountText());
  });

  readonly amountError = computed<string | null>(() => {
    if (this.kind() === 'standing' && this.noCap()) return null;
    const amount = this.amount();
    if (!this.attempted() && !this.amountText()) return null;
    if (amount === null || !Number.isFinite(amount) || amount <= 0) return 'Enter an amount above $0.';
    if (amount > ADMIN_AMOUNT_CEILING) return `Amounts are limited to ${formatAmount(ADMIN_AMOUNT_CEILING)} ${this.per()}.`;
    return null;
  });

  readonly fundingSource = computed<FundingSource | null>(() => {
    const fields = cleanFundingValues(this.fundingValues());
    const label = this.fundingLabel().trim();
    if (!Object.keys(fields).length && !label) return null;
    return { label: label || null, fields, authorizerEmail: this.authorizerEmail().trim() || null };
  });

  readonly fundingComplete = computed(
    () =>
      this.fundingSource() !== null &&
      this.data.policy.fundingFields.every(f => !fundingFieldError(f, this.fundingValues()[f.key])),
  );

  /** "Ask the user" only where the API allows it: request-info is only from pending. */
  readonly canAskForFunding = computed(
    () => this.data.mode === 'approve' && this.data.request.status === 'pending',
  );

  readonly valid = computed(() => {
    if (this.data.mode === 'direct' && (!this.user() || !this.reason().trim())) return false;
    if (this.kind() === 'one_time' || !this.noCap()) {
      const amount = this.amount();
      if (amount === null || !Number.isFinite(amount) || amount <= 0 || amount > ADMIN_AMOUNT_CEILING) return false;
    }
    if (!this.through() || this.through() < this.today) return false;
    if (this.chargeback() && !this.fundingComplete()) return false;
    return true;
  });

  readonly confirmationSentence = computed(() =>
    standingConfirmation({
      user: this.data.mode === 'approve' ? this.data.userLabel : (this.user()?.name || this.user()?.email || 'this user'),
      fund: describeFundingSource(this.fundingSource(), this.data.policy.fundingFields) || 'the funding source',
      cap: this.amount(),
      through: this.through(),
      per: this.per(),
    }),
  );

  constructor() {
    if (this.data.mode === 'approve') {
      const request = this.data.request;
      this.amountText.set(String(toAmount(request.requestedAmount)));
      this.through.set(request.requestedUntil >= this.today ? request.requestedUntil : this.defaultThrough(this.kind()));
      const source = request.fundingSource;
      if (source) {
        this.fundingValues.set({ ...(source.fields ?? {}) });
        this.fundingLabel.set(source.label ?? '');
        this.authorizerEmail.set(source.authorizerEmail ?? '');
      }
    } else {
      this.applyKindDefaults(this.kind());
    }
  }

  setKind(kind: Kind): void {
    if (kind === this.kind()) return;
    this.kind.set(kind);
    this.applyKindDefaults(kind);
  }

  private applyKindDefaults(kind: Kind): void {
    this.through.set(this.defaultThrough(kind));
    if (kind === 'standing') {
      const cap = toAmount(this.data.policy.standingDefaultCap);
      this.noCap.set(!(cap > 0));
      this.amountText.set(cap > 0 ? String(cap) : '');
    } else {
      this.noCap.set(false);
      if (this.data.mode === 'approve') this.amountText.set(String(toAmount(this.data.request.requestedAmount)));
    }
  }

  private defaultThrough(kind: Kind): string {
    const now = new Date();
    if (kind === 'standing') return nextOccurrence(now, this.data.policy.fiscalYearEnd);
    if (this.data.mode === 'approve' && this.data.request.requestedUntil >= this.today) return this.data.request.requestedUntil;
    return endOfPeriod(now, 'monthly');
  }

  inputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  checked(event: Event): boolean {
    return (event.target as HTMLInputElement).checked;
  }

  askForFunding(): void {
    this.dialogRef.close({ kind: 'askForFunding' });
  }

  async submit(): Promise<void> {
    this.attempted.set(true);
    this.error.set(null);
    if (!this.valid() || this.saving()) return;
    if (this.kind() === 'standing' && !this.confirming()) {
      this.confirming.set(true);
      return;
    }

    const standing = this.kind() === 'standing';
    const fundingMode: FundingMode = standing ? 'chargeback' : this.fundingMode();
    const fundingSource = fundingMode === 'chargeback' ? this.fundingSource() : null;
    const note = this.note().trim() || null;

    this.saving.set(true);
    try {
      if (this.data.mode === 'approve') {
        const response = await this.service.approve(this.data.request.requestId, {
          amount: this.amount(),
          validUntil: this.through(),
          fundingMode,
          fundingSource,
          standing,
          note,
        });
        this.dialogRef.close({ kind: 'approved', response });
      } else {
        const extension = await this.service.createExtension({
          userId: this.user()!.userId,
          standing,
          amount: this.amount(),
          validUntil: this.through(),
          fundingMode,
          fundingSource,
          reason: this.reason().trim(),
          note,
        });
        this.dialogRef.close({ kind: 'created', extension });
      }
    } catch (error) {
      this.confirming.set(false);
      this.error.set(quotaAdminErrorMessage(error, 'That did not go through. Try again.'));
    } finally {
      this.saving.set(false);
    }
  }

  cancel(): void {
    this.dialogRef.close(undefined);
  }
}
