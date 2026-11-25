import {
  AbstractControl,
  FormControl,
  FormRecord,
  ValidationErrors,
  ValidatorFn,
  Validators,
} from '@angular/forms';
import type { McpElicitationField } from '../../../../../shared/utils/stream-parser/stream-parser-types';
import type { McpElicitationValue } from '../../../../../services/mcp-elicitation/mcp-elicitation.service';

/**
 * Pure helpers behind `McpElicitationPromptComponent`: building a reactive
 * form from a server's normalised fields, and reading the answer back out in
 * the shape the backend's `validate_answer` accepts
 * (`apis/shared/mcp_elicitation/forms.py`).
 *
 * The backend validates again and sends `cancel` for anything that does not
 * fit, so these rules exist to keep the user from submitting an answer that
 * would be thrown away, and they mirror the backend's rule for rule.
 */

/** A single-select choice with more options than this renders as a `<select>`. */
export const CHIP_CHOICE_LIMIT = 4;

/** What a form control holds, per kind, before it is read back. */
export type McpElicitationControlValue = string | number | boolean | string[] | null;

/** The `<input type>` a text field's `format` asks for. */
export function inputTypeFor(field: McpElicitationField): string {
  switch (field.format) {
    case 'email':
      return 'email';
    case 'uri':
      return 'url';
    case 'date':
      return 'date';
    case 'date-time':
      return 'datetime-local';
    default:
      return 'text';
  }
}

/** How a choice field renders. */
export function choiceControl(field: McpElicitationField): 'chips' | 'select' | 'multi' {
  if (field.multiSelect) return 'multi';
  return (field.options?.length ?? 0) > CHIP_CHOICE_LIMIT ? 'select' : 'chips';
}

function wholeNumber(control: AbstractControl): ValidationErrors | null {
  const value = control.value;
  if (value === null || value === undefined || value === '') return null;
  return Number.isInteger(value) ? null : { integer: true };
}

function finiteNumber(control: AbstractControl): ValidationErrors | null {
  const value = control.value;
  if (value === null || value === undefined || value === '') return null;
  return typeof value === 'number' && Number.isFinite(value) ? null : { number: true };
}

function itemCount(min: number | null | undefined, max: number | null | undefined): ValidatorFn {
  return (control: AbstractControl): ValidationErrors | null => {
    const value = control.value;
    const count = Array.isArray(value) ? value.length : 0;
    // An optional multi-select left empty is omitted from the answer, so its
    // bounds never apply to it.
    if (count === 0) return null;
    if (min !== null && min !== undefined && count < min) return { minItems: min };
    if (max !== null && max !== undefined && count > max) return { maxItems: max };
    return null;
  };
}

/** The server's default, when it is the right type for the field. */
function initialValue(field: McpElicitationField): McpElicitationControlValue {
  const fallback = field.default;
  switch (field.kind) {
    case 'text':
      return typeof fallback === 'string' ? fallback : '';
    case 'number':
    case 'integer':
      return typeof fallback === 'number' && Number.isFinite(fallback) ? fallback : null;
    case 'boolean':
      // A required boolean with no default starts unset (spec §5), so the
      // user has to choose rather than submit a "No" they never picked.
      return typeof fallback === 'boolean' ? fallback : null;
    case 'choice': {
      const offered = new Set((field.options ?? []).map((o) => o.value));
      if (field.multiSelect) {
        return Array.isArray(fallback)
          ? fallback.filter((v): v is string => typeof v === 'string' && offered.has(v))
          : [];
      }
      return typeof fallback === 'string' && offered.has(fallback) ? fallback : null;
    }
  }
}

function validatorsFor(field: McpElicitationField): ValidatorFn[] {
  const validators: ValidatorFn[] = [];
  if (field.required) validators.push(Validators.required);
  switch (field.kind) {
    case 'text':
      if (field.minLength) validators.push(Validators.minLength(field.minLength));
      if (field.maxLength) validators.push(Validators.maxLength(field.maxLength));
      if (field.format === 'email') validators.push(Validators.email);
      break;
    case 'number':
    case 'integer':
      validators.push(finiteNumber);
      if (field.kind === 'integer') validators.push(wholeNumber);
      if (typeof field.minimum === 'number') validators.push(Validators.min(field.minimum));
      if (typeof field.maximum === 'number') validators.push(Validators.max(field.maximum));
      break;
    case 'choice':
      if (field.multiSelect) validators.push(itemCount(field.minItems, field.maxItems));
      break;
    case 'boolean':
      break;
  }
  return validators;
}

/** One control per field, keyed by the field's name. */
export function buildElicitationForm(
  fields: McpElicitationField[],
): FormRecord<FormControl<McpElicitationControlValue>> {
  const form = new FormRecord<FormControl<McpElicitationControlValue>>({});
  for (const field of fields) {
    form.addControl(
      field.name,
      new FormControl<McpElicitationControlValue>(initialValue(field), {
        validators: validatorsFor(field),
      }),
    );
  }
  return form;
}

/**
 * A `datetime-local` value has no zone; the MCP `date-time` format is RFC 3339,
 * which needs one. Read it as the user's local time and send it in UTC.
 */
function toRfc3339(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString();
}

/**
 * The answer's `content`: every field the user filled in, typed by kind.
 *
 * An optional field left empty is omitted, never sent as `""` or `[]`. That
 * is what the backend reads as "not answered", and an empty string sent for an
 * optional field with a `minLength` would be refused there.
 */
export function readElicitationAnswer(
  fields: McpElicitationField[],
  raw: Record<string, McpElicitationControlValue>,
): Record<string, McpElicitationValue> {
  const content: Record<string, McpElicitationValue> = {};
  for (const field of fields) {
    const value = raw[field.name];
    if (value === null || value === undefined) continue;
    if (field.kind === 'text') {
      if (typeof value !== 'string' || value === '') continue;
      content[field.name] = field.format === 'date-time' ? toRfc3339(value) : value;
      continue;
    }
    if (field.kind === 'choice' && field.multiSelect) {
      if (!Array.isArray(value) || value.length === 0) continue;
      content[field.name] = value;
      continue;
    }
    content[field.name] = value;
  }
  return content;
}

/** The first problem with a field, as one short sentence, or null. */
export function fieldError(
  field: McpElicitationField,
  errors: ValidationErrors | null,
): string | null {
  if (!errors) return null;
  if (errors['required']) {
    return field.kind === 'choice' || field.kind === 'boolean'
      ? 'Choose an answer.'
      : 'This is required.';
  }
  if (errors['minlength']) return `Use at least ${errors['minlength'].requiredLength} characters.`;
  if (errors['maxlength']) return `Use at most ${errors['maxlength'].requiredLength} characters.`;
  if (errors['email']) return 'Enter an email address.';
  if (errors['number']) return 'Enter a number.';
  if (errors['integer']) return 'Enter a whole number.';
  if (errors['min']) return `Enter ${errors['min'].min} or more.`;
  if (errors['max']) return `Enter ${errors['max'].max} or less.`;
  if (errors['minItems']) return `Choose at least ${errors['minItems']}.`;
  if (errors['maxItems']) return `Choose at most ${errors['maxItems']}.`;
  return 'Check this answer.';
}

/** `github_issues___delete_repository` → `delete_repository`. */
export function toolLabel(toolName: string): string {
  const cut = toolName.lastIndexOf('___');
  return cut >= 0 ? toolName.slice(cut + 3) : toolName;
}
