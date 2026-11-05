import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { AwsLimit, IncreaseRequestRecord } from '../models';
import { AwsLimitsHttpService, awsLimitsErrorMessage } from '../services/aws-limits-http.service';
import { formatLimitValue, statusLabel, unitAndPeriod } from '../utils/aws-limits-format';

export interface RequestIncreaseDialogData {
  limit: AwsLimit;
  region: string;
  guidanceUrl: string;
}

/** The accepted request, or undefined when the admin closed without submitting. */
export type RequestIncreaseDialogResult = IncreaseRequestRecord | undefined;

type Step = 'form' | 'confirm' | 'done';

const BUTTON_SECONDARY =
  'rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700';
const BUTTON_PRIMARY =
  'rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50';

/**
 * "Request AWS increase": read-only quota facts, a desired value that must exceed
 * the applied one, a lead-time notice, a confirmation step, then AWS's answer.
 */
@Component({
  selector: 'app-request-increase-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Request AWS increase"
      [description]="data.limit.quotaName"
      size="lg"
      (closed)="close()"
    >
      <dl class="grid grid-cols-1 gap-x-6 gap-y-3 text-sm/6 sm:grid-cols-2">
        <div>
          <dt class="text-gray-500 dark:text-gray-400">Service</dt>
          <dd class="font-medium text-gray-900 dark:text-white">{{ data.limit.serviceName }} ({{ data.limit.serviceCode }})</dd>
        </div>
        <div>
          <dt class="text-gray-500 dark:text-gray-400">Region</dt>
          <dd class="font-medium text-gray-900 dark:text-white">{{ data.limit.globalQuota ? 'Global' : data.region }}</dd>
        </div>
        <div>
          <dt class="text-gray-500 dark:text-gray-400">Applied value</dt>
          <dd class="font-medium tabular-nums text-gray-900 dark:text-white" data-testid="applied-value">
            {{ value(data.limit.appliedValue) }} <span class="font-normal text-gray-500 dark:text-gray-400">{{ unit }}</span>
          </dd>
        </div>
        <div>
          <dt class="text-gray-500 dark:text-gray-400">AWS default</dt>
          <dd class="font-medium tabular-nums text-gray-900 dark:text-white">{{ value(data.limit.defaultValue) }}</dd>
        </div>
      </dl>

      @switch (step()) {
        @case ('form') {
          <div class="mt-5">
            <label for="aws-limit-desired" class="block text-sm/6 font-medium text-gray-900 dark:text-white">New value</label>
            <input
              id="aws-limit-desired"
              type="number"
              inputmode="decimal"
              min="0"
              cdkFocusInitial
              [value]="desiredText()"
              (input)="onInput($event)"
              [attr.aria-invalid]="!!validation() && touched()"
              [attr.aria-describedby]="validation() && touched() ? 'aws-limit-desired-error' : 'aws-limit-desired-hint'"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 tabular-nums focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
            />
            @if (validation() && touched()) {
              <p id="aws-limit-desired-error" role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ validation() }}</p>
            } @else {
              <p id="aws-limit-desired-hint" class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">
                Must be greater than {{ value(data.limit.appliedValue) }}.
              </p>
            }
          </div>
          <div class="mt-5 rounded-2xl bg-state-info-50 px-4 py-3 text-sm/6 text-state-info-900 dark:bg-state-info-900/20 dark:text-state-info-200">
            AWS reviews increases on its own schedule. Small ones are often approved in minutes; larger ones,
            and model token-per-minute increases in particular, become an AWS Support case and can take several
            business days. Plan about two weeks for high-demand models.
            <a
              [href]="data.guidanceUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="font-medium underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
            >Read the guidance<span class="sr-only"> (opens in a new tab)</span></a>.
          </div>
        }
        @case ('confirm') {
          <div class="mt-5 rounded-2xl bg-state-warning-50 px-4 py-3 text-sm/6 text-state-warning-900 dark:bg-state-warning-900/20 dark:text-state-warning-200" data-testid="confirmation">
            This asks AWS to raise <strong>{{ data.limit.quotaName }}</strong> from {{ value(data.limit.appliedValue) }}
            to <strong>{{ value(desired()) }}</strong> for the whole AWS account in {{ data.limit.globalQuota ? 'every Region' : data.region }}.
            If AWS needs to review it, Service Quotas opens a support case. The request is recorded under your name in the audit log.
          </div>
        }
        @case ('done') {
          <div class="mt-5 rounded-2xl bg-state-success-50 px-4 py-3 text-sm/6 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200" role="status" data-testid="result">
            AWS accepted the request. Status: <strong>{{ status(result()?.status ?? '') }}</strong>.
            @if (result()?.caseId) {
              Support case <strong>{{ result()?.caseId }}</strong>.
            }
            Follow it in the increase history below or in the Service Quotas console.
          </div>
        }
      }

      @if (error(); as message) {
        <p role="alert" class="mt-4 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
          {{ message }}
        </p>
      }

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        @switch (step()) {
          @case ('form') {
            <button type="button" (click)="close()" [class]="secondary">Cancel</button>
            <button type="button" (click)="review()" [class]="primary">Review request</button>
          }
          @case ('confirm') {
            <button type="button" (click)="back()" [disabled]="saving()" [class]="secondary">Back</button>
            <button type="button" (click)="submit()" [disabled]="saving()" [class]="primary">
              {{ saving() ? 'Submitting…' : 'Submit to AWS' }}
            </button>
          }
          @case ('done') {
            <button type="button" (click)="close()" [class]="primary">Done</button>
          }
        }
      </div>
    </app-dialog-shell>
  `,
})
export class RequestIncreaseDialogComponent {
  private dialogRef = inject<DialogRef<RequestIncreaseDialogResult>>(DialogRef);
  private http = inject(AwsLimitsHttpService);
  readonly data = inject<RequestIncreaseDialogData>(DIALOG_DATA);

  protected readonly primary = BUTTON_PRIMARY;
  protected readonly secondary = BUTTON_SECONDARY;
  protected readonly value = formatLimitValue;
  protected readonly status = statusLabel;
  protected readonly unit = unitAndPeriod(this.data.limit);

  readonly step = signal<Step>('form');
  readonly desiredText = signal('');
  readonly touched = signal(false);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);
  readonly result = signal<IncreaseRequestRecord | null>(null);

  readonly desired = computed(() => {
    const text = this.desiredText().trim();
    return text === '' ? null : Number(text);
  });

  /** Why the value cannot be sent, or null. The server checks the same rules. */
  readonly validation = computed<string | null>(() => {
    const desired = this.desired();
    if (desired === null) return 'Enter the new value.';
    if (!Number.isFinite(desired) || desired <= 0) return 'Enter a positive number.';
    const applied = this.data.limit.appliedValue;
    if (applied !== null && desired <= applied) return `Must be greater than the applied value (${formatLimitValue(applied)}).`;
    return null;
  });

  onInput(event: Event): void {
    this.desiredText.set((event.target as HTMLInputElement).value);
    this.error.set(null);
  }

  review(): void {
    this.touched.set(true);
    if (this.validation()) return;
    this.step.set('confirm');
  }

  back(): void {
    this.error.set(null);
    this.step.set('form');
  }

  async submit(): Promise<void> {
    const desired = this.desired();
    if (this.validation() || desired === null || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const record = await this.http.requestIncrease({
        serviceCode: this.data.limit.serviceCode,
        quotaCode: this.data.limit.quotaCode,
        desiredValue: desired,
      });
      this.result.set(record);
      this.step.set('done');
    } catch (error) {
      this.error.set(awsLimitsErrorMessage(error, 'AWS did not accept the request.'));
    } finally {
      this.saving.set(false);
    }
  }

  close(): void {
    this.dialogRef.close(this.result() ?? undefined);
  }
}
