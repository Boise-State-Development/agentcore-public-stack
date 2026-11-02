import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { FormBuilder, NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { startWith } from 'rxjs';
import { UserSearchResult } from '../../../assistants/models/assistant.model';
import { formatQuotaDate } from '../../../services/quota/quota-format';
import { FundingSourceFieldsComponent } from '../components/funding-source-fields.component';
import { QuotaUserPickerComponent } from '../components/quota-user-picker.component';
import {
  AdminQuotaRequestPolicy,
  FundingField,
  FundingMode,
  FundingSourceMode,
  toAmount,
} from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';

const FIELD_KEY = /^[A-Za-z][A-Za-z0-9_]*$/;
const MONTH_DAY = /^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;

function fieldGroup(fb: NonNullableFormBuilder, field?: Partial<FundingField>) {
  return fb.group({
    key: [field?.key ?? '', [Validators.required, Validators.pattern(FIELD_KEY), Validators.maxLength(40)]],
    label: [field?.label ?? '', [Validators.required, Validators.maxLength(80)]],
    required: [field?.required ?? false],
    pattern: [field?.pattern ?? ''],
    help: [field?.help ?? ''],
  });
}

const INPUT =
  'block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white';

/**
 * The quota request policy (spec §4.5, §6.7): one record that shapes the
 * request form, the approve form and standing chargeback.
 *
 * The funding-field schema editor is a small table with a live preview of
 * what a user (and the approve form) will see. Saving writes one item; an
 * extension keeps the field values it was created with, so changing the
 * schema never makes an old row unreadable. Admins to notify are picked from
 * the users directory, never typed.
 */
@Component({
  selector: 'app-policy-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, FundingSourceFieldsComponent, QuotaUserPickerComponent],
  host: { class: 'block' },
  template: `
    @if (loadError(); as message) {
      <p role="alert" class="rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
    }

    @if (loaded()) {
      <form [formGroup]="form" (ngSubmit)="save()" class="max-w-4xl space-y-8">
        <section aria-labelledby="request-form-heading" class="space-y-4">
          <h2 id="request-form-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">The request form</h2>
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <label for="policy-presets" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Amount presets</label>
              <input id="policy-presets" formControlName="amountPresets" [class]="input" aria-describedby="policy-presets-help" />
              <p id="policy-presets-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">Dollar amounts, comma-separated, e.g. 10, 25, 50.</p>
            </div>
            <div>
              <label for="policy-max" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Most a user may ask for</label>
              <input id="policy-max" type="number" min="0" step="1" formControlName="maxRequestAmount" [class]="input" aria-describedby="policy-max-help" />
              <p id="policy-max-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">0 means no cap. Admins can always grant more.</p>
            </div>
            <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white sm:col-span-2">
              <input type="checkbox" formControlName="allowCustomAmount" class="size-4 rounded-sm border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
              Users may type their own amount
            </label>
            <div>
              <label for="policy-through" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Default end date</label>
              <select id="policy-through" formControlName="throughKind" [class]="input">
                <option value="end_of_period">End of the current period</option>
                <option value="days">A number of days from today</option>
              </select>
            </div>
            @if (values().throughKind === 'days') {
              <div>
                <label for="policy-through-days" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Days from today</label>
                <input id="policy-through-days" type="number" min="1" max="366" formControlName="throughDays" [class]="input" />
              </div>
            }
            <div>
              <label for="policy-duration" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Longest span a request may ask for (days)</label>
              <input id="policy-duration" type="number" min="1" max="366" formControlName="maxDurationDays" [class]="input" />
            </div>
            <div>
              <label for="policy-justification" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Shortest justification (characters)</label>
              <input id="policy-justification" type="number" min="0" max="2000" formControlName="justificationMinChars" [class]="input" />
            </div>
          </div>
        </section>

        <section aria-labelledby="funding-heading" class="space-y-4">
          <h2 id="funding-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Funding sources</h2>
          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">Ask users for a funding source</legend>
            <div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
              @for (mode of fundingModes; track mode.value) {
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="radio" formControlName="fundingSourceMode" [value]="mode.value" class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  {{ mode.label }}
                </label>
              }
            </div>
          </fieldset>

          <div>
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">Funding fields</p>
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">
              The coded parts of a fund, in order (for example Fund, Org, Program). A pattern is a regular expression the whole value must match.
            </p>
            @if (fields.length) {
              <div class="relative mt-2 overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10">
                <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
                  <thead class="bg-gray-50 dark:bg-white/5">
                    <tr>
                      <th scope="col" class="px-3 py-2 font-semibold text-gray-900 dark:text-white">Key</th>
                      <th scope="col" class="px-3 py-2 font-semibold text-gray-900 dark:text-white">Label</th>
                      <th scope="col" class="px-3 py-2 font-semibold text-gray-900 dark:text-white">Required</th>
                      <th scope="col" class="px-3 py-2 font-semibold text-gray-900 dark:text-white">Pattern</th>
                      <th scope="col" class="px-3 py-2 font-semibold text-gray-900 dark:text-white">Help</th>
                      <th scope="col" class="px-3 py-2"><span class="sr-only">Remove</span></th>
                    </tr>
                  </thead>
                  <tbody formArrayName="fundingFields" class="divide-y divide-gray-200 dark:divide-white/10">
                    @for (row of fields.controls; track row; let i = $index) {
                      <tr [formGroupName]="i">
                        <td class="px-3 py-2">
                          <input formControlName="key" [attr.aria-label]="'Key, field ' + (i + 1)" [class]="input"
                            [attr.aria-invalid]="row.get('key')?.invalid && row.get('key')?.touched ? 'true' : null" />
                        </td>
                        <td class="px-3 py-2"><input formControlName="label" [attr.aria-label]="'Label, field ' + (i + 1)" [class]="input" /></td>
                        <td class="px-3 py-2 text-center">
                          <input type="checkbox" formControlName="required" [attr.aria-label]="'Required, field ' + (i + 1)"
                            class="size-4 rounded-sm border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                        </td>
                        <td class="px-3 py-2"><input formControlName="pattern" [attr.aria-label]="'Pattern, field ' + (i + 1)" [class]="input" /></td>
                        <td class="px-3 py-2"><input formControlName="help" [attr.aria-label]="'Help, field ' + (i + 1)" [class]="input" /></td>
                        <td class="px-3 py-2 text-right">
                          <button type="button" (click)="removeField(i)" [attr.aria-label]="'Remove field ' + (i + 1)"
                            class="rounded-2xl px-2 py-1 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 dark:text-state-danger-300 dark:hover:bg-state-danger-900/20">
                            Remove
                          </button>
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>
            }
            <button type="button" (click)="addField()"
              class="mt-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
              Add a field
            </button>
          </div>

          <div class="rounded-2xl border border-dashed border-gray-300 p-4 dark:border-gray-600">
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">Preview</p>
            <p class="mb-3 text-xs/5 text-gray-600 dark:text-gray-400">
              @switch (values().fundingSourceMode) {
                @case ('hidden') { Users are not asked. Admins see these fields when charging back. }
                @case ('required') { Users must fill this in to send a request. }
                @default { Users see this as an optional section. }
              }
            </p>
            <app-funding-source-fields [fields]="previewFields()" />
          </div>

          <div class="grid gap-4 sm:grid-cols-2">
            <fieldset>
              <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">Pre-selected on the approve form</legend>
              <div class="mt-2 flex flex-wrap gap-x-6 gap-y-2">
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="radio" formControlName="defaultFundingMode" value="central" class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  Centrally funded
                </label>
                <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
                  <input type="radio" formControlName="defaultFundingMode" value="chargeback" class="size-4 border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
                  Charge to a funding source
                </label>
              </div>
            </fieldset>
            <div>
              <label for="policy-group" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Report grouping field</label>
              <select id="policy-group" formControlName="reportGroupField" [class]="input">
                <option value="">None</option>
                @for (field of previewFields(); track field.key) {
                  <option [value]="field.key">{{ field.label || field.key }}</option>
                }
              </select>
            </div>
          </div>
        </section>

        <section aria-labelledby="standing-heading" class="space-y-4">
          <h2 id="standing-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Standing chargeback</h2>
          <label class="flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
            <input type="checkbox" formControlName="standingEnabled" aria-describedby="policy-standing-help" class="size-4 rounded-sm border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
            Admins may create standing chargebacks
          </label>
          <p id="policy-standing-help" class="text-xs/5 text-gray-600 dark:text-gray-400">
            Usage past the included quota is charged to the user's fund. Needs at least one funding field.
            @if (values().standingEnabled && !previewFields().length) {
              <span class="font-medium text-state-warning-800 dark:text-state-warning-200">Add a funding field above, or the standing option stays off.</span>
            }
          </p>
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <label for="policy-cap" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Default monthly cap</label>
              <input id="policy-cap" type="number" min="0" step="1" formControlName="standingDefaultCap" [class]="input" aria-describedby="policy-cap-help" />
              <p id="policy-cap-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">0 starts the form at No cap. A cap is the recommendation.</p>
            </div>
            <div>
              <label for="policy-fy" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Fiscal year end (MM-DD)</label>
              <input id="policy-fy" formControlName="fiscalYearEnd" placeholder="06-30" [class]="input" aria-describedby="policy-fy-help"
                [attr.aria-invalid]="form.controls.fiscalYearEnd.invalid ? 'true' : null" />
              <p id="policy-fy-help" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">A standing chargeback ends here by default.</p>
            </div>
          </div>
        </section>

        <section aria-labelledby="notify-heading" class="space-y-3">
          <h2 id="notify-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Who is told about new requests</h2>
          @if (notifyEmails().length) {
            <ul class="flex flex-wrap gap-2">
              @for (email of notifyEmails(); track email) {
                <li class="inline-flex items-center gap-1 rounded-full bg-gray-100 py-0.5 pl-3 pr-1 text-sm/6 text-gray-900 dark:bg-white/10 dark:text-white">
                  {{ email }}
                  <button type="button" (click)="removeNotify(email)" [attr.aria-label]="'Stop notifying ' + email"
                    class="flex size-6 items-center justify-center rounded-full text-gray-500 hover:bg-gray-200 hover:text-gray-800 dark:hover:bg-white/10 dark:hover:text-white">×</button>
                </li>
              }
            </ul>
          } @else {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">No one yet. Requests still appear in the queue and badge.</p>
          }
          <div class="max-w-md">
            <app-quota-user-picker label="Add someone" [clearOnPick]="true" (picked)="addNotify($event)" />
          </div>
        </section>

        @if (saveError(); as message) {
          <p role="alert" class="rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
        }
        @if (saved()) {
          <p role="status" class="rounded-2xl bg-state-success-50 px-4 py-3 text-sm/6 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200">Saved.</p>
        }

        <div class="flex items-center gap-4 border-t border-gray-200 pt-4 dark:border-white/10">
          <button type="submit" [disabled]="saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50">
            {{ saving() ? 'Saving…' : 'Save settings' }}
          </button>
          @if (updatedLine(); as line) {
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">{{ line }}</p>
          }
        </div>
      </form>
    } @else if (!loadError()) {
      <p class="py-10 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading settings…</p>
    }
  `,
})
export class PolicySettingsPage implements OnInit {
  private service = inject(QuotaRequestsAdminService);
  private fb = inject(FormBuilder).nonNullable;

  readonly input = INPUT;
  readonly fundingModes: { value: FundingSourceMode; label: string }[] = [
    { value: 'hidden', label: 'Never' },
    { value: 'optional', label: 'Optional' },
    { value: 'required', label: 'Required' },
  ];

  readonly form = this.fb.group({
    amountPresets: ['10, 25, 50'],
    allowCustomAmount: [true],
    maxRequestAmount: [100, [Validators.required, Validators.min(0)]],
    throughKind: ['end_of_period' as 'end_of_period' | 'days'],
    throughDays: [30, [Validators.min(1), Validators.max(366)]],
    maxDurationDays: [92, [Validators.required, Validators.min(1), Validators.max(366)]],
    justificationMinChars: [20, [Validators.required, Validators.min(0), Validators.max(2000)]],
    fundingSourceMode: ['optional' as FundingSourceMode],
    fundingFields: this.fb.array([] as ReturnType<typeof fieldGroup>[]),
    defaultFundingMode: ['central' as FundingMode],
    standingEnabled: [true],
    standingDefaultCap: [100, [Validators.required, Validators.min(0)]],
    fiscalYearEnd: ['12-31', [Validators.required, Validators.pattern(MONTH_DAY)]],
    reportGroupField: [''],
  });

  get fields() {
    return this.form.controls.fundingFields;
  }

  readonly values = toSignal(this.form.valueChanges.pipe(startWith(this.form.getRawValue())), {
    initialValue: this.form.getRawValue(),
  });

  readonly previewFields = computed<FundingField[]>(() =>
    (this.values().fundingFields ?? [])
      .filter(f => (f.key ?? '').trim())
      .map(f => ({
        key: (f.key ?? '').trim(),
        label: (f.label ?? '').trim() || (f.key ?? '').trim(),
        required: !!f.required,
        pattern: (f.pattern ?? '').trim() || null,
        help: (f.help ?? '').trim() || null,
      })),
  );

  readonly notifyEmails = signal<string[]>([]);
  readonly loaded = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly saving = signal(false);
  readonly saved = signal(false);
  readonly saveError = signal<string | null>(null);
  private readonly updated = signal<{ by?: string | null; at?: string | null }>({});

  readonly updatedLine = computed(() => {
    const { at } = this.updated();
    return at ? `Last saved ${formatQuotaDate(at)}` : '';
  });

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    try {
      this.apply(await this.service.getPolicy());
      this.loaded.set(true);
    } catch (error) {
      this.loadError.set(quotaAdminErrorMessage(error, 'The quota request settings could not be loaded.'));
    }
  }

  private apply(policy: AdminQuotaRequestPolicy): void {
    const days = /^days:(\d+)$/.exec(policy.defaultThrough);
    this.fields.clear();
    for (const field of policy.fundingFields) this.fields.push(fieldGroup(this.fb, field));
    this.form.patchValue({
      amountPresets: policy.amountPresets.map(p => toAmount(p)).join(', '),
      allowCustomAmount: policy.allowCustomAmount,
      maxRequestAmount: toAmount(policy.maxRequestAmount),
      throughKind: days ? 'days' : 'end_of_period',
      throughDays: days ? Number(days[1]) : 30,
      maxDurationDays: policy.maxDurationDays,
      justificationMinChars: policy.justificationMinChars,
      fundingSourceMode: policy.fundingSourceMode,
      defaultFundingMode: policy.defaultFundingMode,
      standingEnabled: policy.standingEnabled,
      standingDefaultCap: toAmount(policy.standingDefaultCap),
      fiscalYearEnd: policy.fiscalYearEnd,
      reportGroupField: policy.reportGroupField ?? '',
    });
    this.notifyEmails.set([...policy.notifyEmails]);
    this.updated.set({ by: policy.updatedBy, at: policy.updatedAt });
  }

  addField(): void {
    this.fields.push(fieldGroup(this.fb));
  }

  removeField(index: number): void {
    const key = this.fields.at(index).value.key;
    this.fields.removeAt(index);
    if (key && this.form.controls.reportGroupField.value === key) {
      this.form.controls.reportGroupField.setValue('');
    }
  }

  addNotify(user: UserSearchResult): void {
    const email = user?.email?.toLowerCase();
    if (!email) return;
    this.notifyEmails.update(list => (list.includes(email) ? list : [...list, email]));
  }

  removeNotify(email: string): void {
    this.notifyEmails.update(list => list.filter(e => e !== email));
  }

  /** The presets text as numbers, or null when any entry is not a positive amount. */
  private presets(): number[] | null {
    const parts = this.form.controls.amountPresets.value.split(',').map(p => p.trim()).filter(Boolean);
    const amounts = parts.map(p => Number(p.replace(/^\$/, '')));
    return amounts.every(a => Number.isFinite(a) && a > 0) ? amounts : null;
  }

  async save(): Promise<void> {
    this.saved.set(false);
    this.saveError.set(null);
    this.form.markAllAsTouched();
    const presets = this.presets();
    if (presets === null) {
      this.saveError.set('Amount presets must be positive dollar amounts, separated by commas.');
      return;
    }
    if (this.form.invalid) {
      this.saveError.set(this.firstProblem());
      return;
    }
    const value = this.form.getRawValue();
    const policy: AdminQuotaRequestPolicy = {
      amountPresets: presets.map(String),
      allowCustomAmount: value.allowCustomAmount,
      maxRequestAmount: String(value.maxRequestAmount),
      defaultThrough: value.throughKind === 'days' ? `days:${value.throughDays}` : 'end_of_period',
      maxDurationDays: value.maxDurationDays,
      justificationMinChars: value.justificationMinChars,
      fundingSourceMode: value.fundingSourceMode,
      fundingFields: this.previewFields(),
      defaultFundingMode: value.defaultFundingMode,
      standingEnabled: value.standingEnabled,
      standingDefaultCap: String(value.standingDefaultCap),
      fiscalYearEnd: value.fiscalYearEnd,
      reportGroupField: value.reportGroupField || null,
      notifyEmails: this.notifyEmails(),
    };
    this.saving.set(true);
    try {
      this.apply(await this.service.updatePolicy(policy));
      this.saved.set(true);
    } catch (error) {
      this.saveError.set(quotaAdminErrorMessage(error, 'The settings could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }

  private firstProblem(): string {
    const c = this.form.controls;
    if (this.fields.invalid) return 'Each funding field needs a key (a letter, then letters, digits or _) and a label.';
    if (c.fiscalYearEnd.invalid) return 'The fiscal year end is a month and day, like 06-30.';
    if (c.maxDurationDays.invalid || c.throughDays.invalid) return 'Durations are between 1 and 366 days.';
    if (c.justificationMinChars.invalid) return 'The shortest justification is between 0 and 2000 characters.';
    return 'Amounts cannot be negative.';
  }
}
