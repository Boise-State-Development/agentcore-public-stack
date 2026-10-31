import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';
import { FundingField } from '../models/quota-requests-admin.models';
import { fundingFieldError } from '../utils/quota-admin-copy';

let nextId = 0;

/**
 * A funding source's inputs, drawn from the deployment's schema (spec §4.3).
 *
 * One input per schema field, then the free-text label and the authorizer.
 * The approve form, the direct-extension form and the Settings preview all
 * draw it, so the preview is exactly what the form will show.
 */
@Component({
  selector: 'app-funding-source-fields',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    <div class="grid gap-3 sm:grid-cols-2">
      @for (field of fields(); track field.key) {
        <div>
          <label [for]="id(field.key)" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
            {{ field.label }}
            @if (field.required) {
              <span class="text-state-danger-700 dark:text-state-danger-300" aria-hidden="true">*</span>
            }
          </label>
          <input
            [id]="id(field.key)"
            type="text"
            [value]="values()[field.key] || ''"
            (input)="setValue(field.key, $event)"
            [attr.aria-required]="field.required ? 'true' : null"
            [attr.aria-invalid]="errorFor(field) ? 'true' : null"
            [attr.aria-describedby]="describedBy(field)"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          />
          @if (field.help) {
            <p [id]="id(field.key) + '-help'" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">{{ field.help }}</p>
          }
          @if (errorFor(field); as message) {
            <p [id]="id(field.key) + '-error'" class="mt-1 text-xs/5 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
          }
        </div>
      }
      <div>
        <label [for]="id('label')" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Name of the fund (optional)</label>
        <input
          [id]="id('label')"
          type="text"
          [value]="label()"
          (input)="label.set(inputValue($event))"
          class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
        />
      </div>
      <div>
        <label [for]="id('authorizer')" class="block text-sm/6 font-medium text-gray-900 dark:text-white">Authorizer email (optional)</label>
        <input
          [id]="id('authorizer')"
          type="email"
          [value]="authorizerEmail()"
          (input)="authorizerEmail.set(inputValue($event))"
          class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
        />
      </div>
    </div>
  `,
})
export class FundingSourceFieldsComponent {
  readonly fields = input<FundingField[]>([]);
  /** Show "required" and pattern errors; off until the admin has tried to submit. */
  readonly showErrors = input(false);
  readonly values = model<Record<string, string>>({});
  readonly label = model('');
  readonly authorizerEmail = model('');

  private readonly prefix = `funding-${nextId++}`;

  id(key: string): string {
    return `${this.prefix}-${key}`;
  }

  inputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  setValue(key: string, event: Event): void {
    this.values.update(values => ({ ...values, [key]: this.inputValue(event) }));
  }

  errorFor(field: FundingField): string | null {
    const value = this.values()[field.key];
    // A pattern mismatch shows as soon as there is a value; "required" waits for a submit.
    if (!this.showErrors() && !(value ?? '').trim()) return null;
    return fundingFieldError(field, value);
  }

  describedBy(field: FundingField): string | null {
    const ids = [
      field.help ? `${this.id(field.key)}-help` : '',
      this.errorFor(field) ? `${this.id(field.key)}-error` : '',
    ].filter(Boolean);
    return ids.length ? ids.join(' ') : null;
  }
}
