import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../dialog/dialog-shell.component';
import { SpinnerComponent } from '../spinner/spinner.component';
import { QuotaStatusService } from '../../services/quota/quota-status.service';
import { QuotaRequestFailure, QuotaRequestService } from '../../services/quota/quota-request.service';
import {
  FundingField,
  FundingSource,
  QuotaRequestPolicy,
  toAmount,
} from '../../services/quota/quota-request.model';
import {
  addDays,
  endOfPeriod,
  endsMidMonth,
  formatAmount,
  formatQuotaDate,
  formatUsd,
  nextOccurrence,
  perPeriod,
  periodAdjective,
} from '../../services/quota/quota-format';

export interface QuotaRequestDialogData {
  /**
   * Set when the admin asked for a funding source (`info_requested`): the dialog
   * opens on the funding section with their note, and sending it returns the
   * request to pending.
   */
  fundingFor?: { requestId: string; adminNote?: string | null };
}

/** What happened; `undefined` means the user closed the dialog without sending anything. */
export type QuotaRequestDialogResult = 'submitted' | 'funding_added' | 'withdrawn' | undefined;

const JUSTIFICATION_MAX = 2000;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const FIELD_CLASS =
  'block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-400';

/**
 * Request more quota (quota-requests spec §5.2), and the reply to an admin's
 * request for a funding source (§5.4). All three entry points open this one
 * dialog through `QuotaRequestDialogService`.
 *
 * Everything it offers comes from `GET /quota/policy`; the backend validates
 * the same rules again, and a refusal it sends is shown here verbatim.
 */
@Component({
  selector: 'app-quota-request-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, SpinnerComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="fundingMode ? 'Add a funding source' : 'Request more quota'" (closed)="close()">
      @if (sent(); as outcome) {
        <p class="text-sm/6 text-gray-700 dark:text-gray-300" role="status">
          @if (outcome === 'funding_added') {
            Sent. Your request is back with your administrator.
          } @else {
            Sent. You’ll see the decision here and in your notifications.
          }
        </p>
      } @else if (policyError()) {
        <p class="text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ policyError() }}</p>
      } @else if (!policy()) {
        <div class="flex justify-center py-8">
          <app-spinner size="md" label="Loading" />
        </div>
      } @else {
        <div>
          @if (headerLine(); as line) {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400" data-testid="quota-request-header">{{ line }}</p>
          }

          @if (fundingMode) {
            @if (data?.fundingFor?.adminNote; as note) {
              <div class="mt-3 rounded-2xl bg-gray-50 px-4 py-3 text-sm/6 text-gray-800 dark:bg-white/5 dark:text-gray-200">
                <span class="font-medium">Your administrator asked:</span>
                <span class="italic"> “{{ note }}”</span>
              </div>
            }
          } @else {
            <fieldset class="mt-5">
              <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">
                {{ standing() ? 'What cap do you need?' : 'How much more do you need?' }}
              </legend>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                @for (preset of presets(); track preset) {
                  <label
                    class="cursor-pointer rounded-2xl border px-3 py-1.5 text-sm/6 font-medium has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-primary-500"
                    [class]="amountChoice() === preset.toString()
                      ? 'border-primary-accessible bg-primary-accessible text-white'
                      : 'border-gray-300 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700'"
                  >
                    <input
                      type="radio"
                      name="quota-request-amount"
                      class="sr-only"
                      [value]="preset.toString()"
                      [checked]="amountChoice() === preset.toString()"
                      (change)="amountChoice.set(preset.toString())"
                    />
                    {{ amountLabel(preset) }}
                  </label>
                }
                @if (policy()!.allowCustomAmount) {
                  <label
                    class="flex cursor-pointer items-center gap-1.5 rounded-2xl border px-3 py-1 text-sm/6 font-medium has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-primary-500"
                    [class]="amountChoice() === 'other'
                      ? 'border-primary-accessible text-gray-900 dark:text-white'
                      : 'border-gray-300 text-gray-700 dark:border-gray-600 dark:text-gray-200'"
                  >
                    <input
                      type="radio"
                      name="quota-request-amount"
                      class="sr-only"
                      value="other"
                      [checked]="amountChoice() === 'other'"
                      (change)="amountChoice.set('other')"
                    />
                    Other
                  </label>
                  @if (amountChoice() === 'other') {
                    <div class="flex items-center gap-1">
                      <span class="text-sm/6 text-gray-700 dark:text-gray-300" aria-hidden="true">$</span>
                      <input
                        id="quota-request-custom"
                        type="number"
                        inputmode="decimal"
                        min="1"
                        step="1"
                        aria-label="Other amount in dollars"
                        [attr.aria-invalid]="amountError() ? 'true' : null"
                        [attr.aria-describedby]="amountError() ? 'quota-request-amount-error' : null"
                        [value]="customAmount()"
                        (input)="customAmount.set(value($event))"
                        class="w-24 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                      />
                    </div>
                  }
                }
              </div>
              @if (amountError(); as error) {
                <p id="quota-request-amount-error" class="mt-1.5 text-xs/5 text-state-danger-700 dark:text-state-danger-300">{{ error }}</p>
              }
            </fieldset>

            <div class="mt-4">
              <label for="quota-request-through" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                {{ throughLabel() }}
              </label>
              <input
                id="quota-request-through"
                type="date"
                [min]="today"
                [attr.max]="standing() ? null : maxThrough()"
                [value]="through()"
                (input)="through.set(value($event))"
                [attr.aria-describedby]="throughHint() ? 'quota-request-through-hint' : null"
                class="mt-1.5 w-48 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:[color-scheme:dark]"
              />
              @if (throughHint(); as hint) {
                <p id="quota-request-through-hint" class="mt-1.5 text-xs/5" [class]="throughInvalid() ? 'text-state-danger-700 dark:text-state-danger-300' : 'text-gray-600 dark:text-gray-400'">{{ hint }}</p>
              }
            </div>

            <div class="mt-4">
              <label for="quota-request-why" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Why do you need it?
              </label>
              <textarea
                id="quota-request-why"
                rows="3"
                cdkFocusInitial
                [attr.maxlength]="justificationMax"
                aria-describedby="quota-request-why-hint"
                [value]="justification()"
                (input)="justification.set(value($event))"
                [class]="fieldClass + ' mt-1.5'"
              ></textarea>
              <p id="quota-request-why-hint" class="mt-1.5 text-xs/5 text-gray-600 dark:text-gray-400">
                A sentence or two helps your administrator decide quickly.
                @if (justificationShort() > 0) {
                  ({{ justificationShort() }} more characters)
                }
              </p>
            </div>
          }

          @if (showFunding()) {
            <div class="mt-5 rounded-2xl border border-gray-200 dark:border-gray-700">
              @if (fundingCollapsible()) {
                <button
                  type="button"
                  class="flex w-full items-center gap-2 rounded-2xl px-4 py-2.5 text-left text-sm/6 font-medium text-gray-900 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-white dark:hover:bg-white/5"
                  [attr.aria-expanded]="fundingOpen()"
                  aria-controls="quota-request-funding"
                  (click)="fundingOpen.set(!fundingOpen())"
                >
                  <span aria-hidden="true" class="inline-block transition-transform" [class.rotate-90]="fundingOpen()">▸</span>
                  I have a funding source to charge this to (optional)
                </button>
              } @else {
                <p class="px-4 pt-3 text-sm/6 font-medium text-gray-900 dark:text-white">Funding source</p>
              }
              @if (fundingOpen() || !fundingCollapsible()) {
                <div id="quota-request-funding" class="grid gap-3 px-4 pt-2 pb-4 sm:grid-cols-2">
                  @for (field of fundingFields(); track field.key) {
                    <div>
                      <label [for]="'quota-funding-' + field.key" class="block text-xs/5 font-medium text-gray-800 dark:text-gray-200">
                        {{ field.label }}@if (field.required && fundingRequired()) {<span aria-hidden="true"> *</span>}
                      </label>
                      <input
                        [id]="'quota-funding-' + field.key"
                        [attr.cdkFocusInitial]="fundingMode && $first ? '' : null"
                        type="text"
                        [attr.aria-required]="field.required && fundingRequired() ? 'true' : null"
                        [attr.aria-invalid]="fieldInvalid(field) ? 'true' : null"
                        [attr.aria-describedby]="field.help || fieldInvalid(field) ? 'quota-funding-' + field.key + '-hint' : null"
                        [value]="fundingValue(field.key)"
                        (input)="setFundingValue(field.key, value($event))"
                        [class]="fieldClass + ' mt-1'"
                      />
                      @if (field.help || fieldInvalid(field)) {
                        <p [id]="'quota-funding-' + field.key + '-hint'" class="mt-1 text-xs/5" [class]="fieldInvalid(field) ? 'text-state-danger-700 dark:text-state-danger-300' : 'text-gray-600 dark:text-gray-400'">
                          {{ fieldInvalid(field) ? 'This doesn’t match the expected format.' : field.help }}
                        </p>
                      }
                    </div>
                  }
                  <div class="sm:col-span-2">
                    <label for="quota-funding-label" class="block text-xs/5 font-medium text-gray-800 dark:text-gray-200">
                      {{ fundingFields().length ? 'Name of the fund (optional)' : 'Which fund or account should this be charged to?' }}
                    </label>
                    <input
                      id="quota-funding-label"
                      [attr.cdkFocusInitial]="fundingMode && !fundingFields().length ? '' : null"
                      type="text"
                      maxlength="200"
                      [attr.aria-required]="!fundingFields().length && fundingRequired() ? 'true' : null"
                      [value]="fundingLabel()"
                      (input)="fundingLabel.set(value($event))"
                      [class]="fieldClass + ' mt-1'"
                    />
                  </div>
                  <div class="sm:col-span-2">
                    <label for="quota-funding-authorizer" class="block text-xs/5 font-medium text-gray-800 dark:text-gray-200">
                      Who authorizes this fund? (email)
                    </label>
                    <input
                      id="quota-funding-authorizer"
                      type="email"
                      [attr.aria-invalid]="authorizerInvalid() ? 'true' : null"
                      [value]="authorizerEmail()"
                      (input)="authorizerEmail.set(value($event))"
                      [class]="fieldClass + ' mt-1'"
                    />
                  </div>
                  @if (!fundingMode && policy()!.standingEnabled) {
                    <label class="flex items-start gap-2 text-sm/6 text-gray-800 sm:col-span-2 dark:text-gray-200">
                      <input
                        type="checkbox"
                        class="mt-1 size-4 rounded border-gray-300 text-primary-accessible focus:ring-primary-accessible dark:border-gray-600"
                        [checked]="standing()"
                        (change)="setStanding(checked($event))"
                      />
                      <span>
                        Make this ongoing
                        <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                          The amount becomes a cap {{ periodWord() }}, charged to this fund, through the end of the fiscal year.
                        </span>
                      </span>
                    </label>
                  }
                </div>
              }
            </div>
          }

          @if (error(); as message) {
            <p class="mt-4 text-sm/6 text-state-danger-700 dark:text-state-danger-300" role="alert">{{ message }}</p>
          }

        </div>
      }

      @if (sent()) {
        <div dialogFooter class="flex justify-end border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button type="button" cdkFocusInitial (click)="close()" [class]="primaryButton">Done</button>
        </div>
      } @else if (policy()) {
        <div dialogFooter class="flex flex-wrap items-center justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          @if (fundingMode) {
            <button
              type="button"
              (click)="withdraw()"
              [disabled]="submitting()"
              class="mr-auto rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              Withdraw request
            </button>
          }
          <button
            type="button"
            (click)="close()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button type="button" (click)="send()" [disabled]="!canSend() || submitting()" [class]="primaryButton">
            @if (submitting()) {
              <app-spinner size="sm" variant="on-solid" label="Sending" />
            }
            {{ fundingMode ? 'Send funding source' : 'Send request' }}
          </button>
        </div>
      }
    </app-dialog-shell>
  `,
})
export class QuotaRequestDialogComponent {
  private dialogRef = inject<DialogRef<QuotaRequestDialogResult>>(DialogRef);
  protected readonly data = inject<QuotaRequestDialogData | null>(DIALOG_DATA, { optional: true });
  private requests = inject(QuotaRequestService);
  private quotaStatus = inject(QuotaStatusService);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);

  protected readonly fundingMode = !!this.data?.fundingFor;
  protected readonly fieldClass = FIELD_CLASS;
  protected readonly justificationMax = JUSTIFICATION_MAX;
  protected readonly primaryButton =
    'inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:brightness-110';

  private readonly now = new Date();
  protected readonly today = addDays(this.now, 0);

  protected readonly policy = signal<QuotaRequestPolicy | null>(null);
  protected readonly policyError = signal<string | null>(null);
  private readonly status = computed(() => this.quotaStatus.status.value() ?? null);

  protected readonly amountChoice = signal<string>('');
  protected readonly customAmount = signal('');
  protected readonly through = signal('');
  protected readonly justification = signal('');
  protected readonly standing = signal(false);
  protected readonly fundingOpen = signal(false);
  protected readonly fundingValues = signal<Record<string, string>>({});
  protected readonly authorizerEmail = signal('');
  protected readonly fundingLabel = signal('');
  protected readonly submitting = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly sent = signal<QuotaRequestDialogResult>(undefined);

  protected readonly periodWord = computed(() => perPeriod(this.status()?.periodType));
  protected readonly throughLabel = computed(() => {
    const word = this.periodWord();
    return `${word.charAt(0).toUpperCase()}${word.slice(1)}, through`;
  });
  protected readonly presets = computed(() =>
    (this.policy()?.amountPresets ?? []).map(toAmount).filter(n => Number.isFinite(n) && n > 0),
  );
  private readonly maxAmount = computed(() => toAmount(this.policy()?.maxRequestAmount ?? 0) || 0);
  protected readonly maxThrough = computed(() => addDays(this.now, this.policy()?.maxDurationDays ?? 92));
  protected readonly fundingFields = computed<FundingField[]>(() => this.policy()?.fundingFields ?? []);

  /** The live quota status, so the user never guesses (§5.2). */
  protected readonly headerLine = computed(() => {
    const status = this.status();
    if (!status?.configured || status.unlimited) return null;
    const standing = (status.extensions ?? []).find(e => e.standing);
    if (standing && status.baseLimit != null) {
      const cap = standing.amount != null ? `, up to ${formatAmount(standing.amount)} ${perPeriod(status.periodType)}` : '';
      const fund = standing.fundingLabel ? ` to ${standing.fundingLabel}` : '';
      return `After your ${formatAmount(status.baseLimit)} included quota, usage is charged${fund}${cap}, through ${formatQuotaDate(standing.validUntil)}.`;
    }
    if (status.monthlyLimit == null) return null;
    const reset = status.resetInfo ? ` · ${status.resetInfo}` : '';
    return `You’ve used ${formatUsd(status.currentUsage)} of your ${formatUsd(status.monthlyLimit)} ${periodAdjective(status.periodType)} quota${reset}`;
  });

  protected readonly amount = computed(() => {
    const choice = this.amountChoice();
    if (choice === 'other') return toAmount(this.customAmount().trim());
    return toAmount(choice);
  });

  protected readonly amountError = computed(() => {
    if (this.amountChoice() !== 'other' || this.customAmount().trim() === '') return null;
    const amount = this.amount();
    if (!Number.isFinite(amount) || amount <= 0) return 'Enter an amount above $0.';
    const max = this.maxAmount();
    if (max > 0 && amount > max) {
      return `Requests are limited to ${formatAmount(max)} ${this.periodWord()}; your administrator can grant more.`;
    }
    return null;
  });

  protected readonly throughInvalid = computed(() => {
    const date = this.through();
    if (!date || date < this.today) return true;
    return !this.standing() && date > this.maxThrough();
  });

  protected readonly throughHint = computed(() => {
    const date = this.through();
    if (!date) return 'Pick an end date.';
    if (date < this.today) return 'Pick a date from today on.';
    if (!this.standing() && date > this.maxThrough()) {
      return `Requests can cover at most ${this.policy()?.maxDurationDays ?? 92} days.`;
    }
    const status = this.status();
    if (status?.periodType !== 'daily' && endsMidMonth(date) && status?.baseLimit != null) {
      return `Your quota drops back to ${formatAmount(status.baseLimit)} on that date, mid-month.`;
    }
    return null;
  });

  protected readonly justificationShort = computed(() =>
    Math.max(0, (this.policy()?.justificationMinChars ?? 0) - this.justification().trim().length),
  );

  /**
   * The request form offers a funding section only where the deployment defines
   * fields. A reply to an admin's ask always has one: with no schema, the fund's
   * name alone is a funding source the backend accepts.
   */
  protected readonly showFunding = computed(() => {
    const policy = this.policy();
    if (!policy) return false;
    return this.fundingMode || (policy.fundingSourceMode !== 'hidden' && policy.fundingFields.length > 0);
  });

  /** The admin asked for it, the deployment requires it, or the request is ongoing. */
  protected readonly fundingRequired = computed(
    () => this.fundingMode || this.standing() || this.policy()?.fundingSourceMode === 'required',
  );

  protected readonly fundingCollapsible = computed(() => !this.fundingRequired());

  protected readonly authorizerInvalid = computed(() => {
    const email = this.authorizerEmail().trim();
    return email !== '' && !EMAIL.test(email);
  });

  private readonly fundingTouched = computed(
    () =>
      Object.values(this.fundingValues()).some(v => v.trim() !== '') ||
      this.fundingLabel().trim() !== '' ||
      this.authorizerEmail().trim() !== '',
  );

  private readonly fundingValid = computed(() => {
    if (this.authorizerInvalid()) return false;
    if (this.fundingFields().some(f => this.fieldInvalid(f))) return false;
    if (!this.fundingRequired() && !this.fundingTouched()) return true;
    const values = this.fundingValues();
    if (!this.fundingFields().length) return this.fundingLabel().trim() !== '';
    return this.fundingFields().every(f => !f.required || (values[f.key] ?? '').trim() !== '');
  });

  protected readonly canSend = computed(() => {
    if (!this.policy()) return false;
    if (this.fundingMode) return this.fundingValid() && this.fundingTouched();
    const amount = this.amount();
    return (
      Number.isFinite(amount) &&
      amount > 0 &&
      !this.amountError() &&
      !this.throughInvalid() &&
      this.justificationShort() === 0 &&
      this.fundingValid()
    );
  });

  constructor() {
    void this.loadPolicy();
  }

  private async loadPolicy(): Promise<void> {
    try {
      const policy = await this.requests.policy();
      this.policy.set(policy);
      const presets = policy.amountPresets.map(toAmount).filter(n => Number.isFinite(n) && n > 0);
      if (presets.length) {
        this.amountChoice.set(presets[Math.min(1, presets.length - 1)].toString());
      } else if (policy.allowCustomAmount) {
        this.amountChoice.set('other');
      }
      this.through.set(this.defaultThrough(policy));
      this.fundingOpen.set(policy.fundingSourceMode === 'required');
      this.focusFirstField();
    } catch {
      this.policyError.set('Quota requests aren’t available right now. Try again later.');
    }
  }

  /**
   * CDK focuses the dialog when it opens, before the policy has loaded and the
   * form exists, so focus lands on the close button. Move it to the form's first
   * field once it renders, unless the user has already moved it themselves.
   */
  private focusFirstField(): void {
    afterNextRender(
      () => {
        const host = this.host.nativeElement;
        const target = host.querySelector<HTMLElement>('[cdkFocusInitial]');
        const active = document.activeElement as HTMLElement | null;
        const untouched = !active || !host.contains(active) || active.getAttribute('aria-label') === 'Close dialog';
        if (target && untouched) target.focus();
      },
      { injector: this.injector },
    );
  }

  private defaultThrough(policy: QuotaRequestPolicy): string {
    const days = /^days:(\d+)$/.exec(policy.defaultThrough);
    if (days) return addDays(this.now, Math.min(Number(days[1]), policy.maxDurationDays));
    return endOfPeriod(this.now, this.status()?.periodType);
  }

  protected amountLabel(amount: number): string {
    return formatAmount(amount);
  }

  protected value(event: Event): string {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }

  protected checked(event: Event): boolean {
    return (event.target as HTMLInputElement).checked;
  }

  protected fundingValue(key: string): string {
    return this.fundingValues()[key] ?? '';
  }

  protected setFundingValue(key: string, value: string): void {
    this.fundingValues.update(values => ({ ...values, [key]: value }));
  }

  protected fieldInvalid(field: FundingField): boolean {
    const value = (this.fundingValues()[field.key] ?? '').trim();
    if (!value || !field.pattern) return false;
    try {
      return !new RegExp(`^(?:${field.pattern})$`).test(value);
    } catch {
      return false;
    }
  }

  /** Ongoing: the amount becomes a cap, the date the fiscal year end, and a fund is required. */
  protected setStanding(on: boolean): void {
    this.standing.set(on);
    const policy = this.policy();
    if (!policy) return;
    this.through.set(on ? nextOccurrence(this.now, policy.fiscalYearEnd) : this.defaultThrough(policy));
  }

  private fundingSource(): FundingSource | null {
    if (!this.fundingTouched()) return null;
    const fields: Record<string, string> = {};
    for (const field of this.fundingFields()) {
      const value = (this.fundingValues()[field.key] ?? '').trim();
      if (value) fields[field.key] = value;
    }
    const email = this.authorizerEmail().trim();
    const label = this.fundingLabel().trim();
    return { fields, ...(label ? { label } : {}), ...(email ? { authorizerEmail: email } : {}) };
  }

  protected async send(): Promise<void> {
    if (!this.canSend() || this.submitting()) return;
    this.submitting.set(true);
    this.error.set(null);
    try {
      const fundingFor = this.data?.fundingFor;
      if (fundingFor) {
        await this.requests.addFundingSource(fundingFor.requestId, this.fundingSource()!);
        this.sent.set('funding_added');
      } else {
        await this.requests.submit({
          kind: this.standing() ? 'standing' : 'one_time',
          requestedAmount: this.amount(),
          requestedUntil: this.through(),
          justification: this.justification().trim(),
          fundingSource: this.fundingSource(),
        });
        this.sent.set('submitted');
      }
    } catch (e) {
      this.error.set(e instanceof QuotaRequestFailure ? e.message : 'Something went wrong. Try again.');
    } finally {
      this.submitting.set(false);
    }
  }

  protected async withdraw(): Promise<void> {
    const requestId = this.data?.fundingFor?.requestId;
    if (!requestId || this.submitting()) return;
    this.submitting.set(true);
    this.error.set(null);
    try {
      await this.requests.withdraw(requestId);
      this.dialogRef.close('withdrawn');
    } catch (e) {
      this.error.set(e instanceof QuotaRequestFailure ? e.message : 'Something went wrong. Try again.');
    } finally {
      this.submitting.set(false);
    }
  }

  protected close(): void {
    this.dialogRef.close(this.sent());
  }
}
