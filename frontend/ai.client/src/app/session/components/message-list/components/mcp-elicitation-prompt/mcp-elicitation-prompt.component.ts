import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { ReactiveFormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import {
  McpElicitationRequest,
  McpElicitationService,
} from '../../../../../services/mcp-elicitation/mcp-elicitation.service';
import type { McpElicitationField } from '../../../../../shared/utils/stream-parser/stream-parser-types';
import { SpinnerComponent } from '../../../../../components/spinner/spinner.component';
import {
  McpElicitationControlValue,
  buildElicitationForm,
  choiceControl,
  fieldError,
  inputTypeFor,
  readElicitationAnswer,
  toolLabel,
} from './mcp-elicitation-form';

let nextFormId = 0;

/**
 * The form an external MCP server paused its tool call to ask the user to
 * fill in (`docs/specs/mcp-elicitation.md` §5).
 *
 * Shares `UserQuestionPromptComponent`'s card language (full width, soft
 * ground, rounded option rows) but not its state machine, and it differs in
 * the one way that matters: **the words come from a third-party server, not
 * from the assistant.** The header names the server (the catalog's display
 * name, which the server cannot set) and says it is the one asking, so the
 * form never reads as the assistant speaking. The server's message is shown
 * as plain text, never rendered as markdown.
 *
 * There is no "Other" and no free-text escape: the answer is typed and closed,
 * and goes to the server as `accept` with content. Decline sends `decline`.
 * Leaving the form unanswered sends nothing (spec §9.5): a new message
 * abandons the paused turn as it does for every other prompt.
 *
 * House traps (see `UserQuestionPromptComponent`): dark rules use
 * `:host-context(.dark)`, and colour goes on an icon's wrapper, not the
 * `<ng-icon>` itself.
 */
@Component({
  selector: 'app-mcp-elicitation-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ReactiveFormsModule, SpinnerComponent],
  providers: [provideIcons({ heroCheck, heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <section
      class="elicit-card w-full rounded-2xl bg-gray-50/80 p-5 ring-1 ring-gray-200/70 dark:bg-white/[0.035] dark:ring-white/10"
      [attr.aria-labelledby]="ids.heading"
    >
      <header class="mb-4">
        <p class="eyebrow">Input requested</p>
        <h3 [id]="ids.heading" class="mt-2 text-sm/6 font-medium text-gray-900 dark:text-gray-50">
          {{ serverName() }} is asking for this
          @if (tool()) {
            <span class="font-normal text-gray-600 dark:text-gray-300">
              while running <code class="tool-name">{{ tool() }}</code></span
            >
          }
        </h3>
        @if (request().message) {
          <p class="server-message mt-3 text-sm/6 text-gray-800 dark:text-gray-100">
            {{ request().message }}
          </p>
        }
      </header>

      <form [formGroup]="form()" (ngSubmit)="submit()" novalidate class="grid gap-4">
        @for (field of request().fields; track field.name) {
          @let control = form().controls[field.name];
          @let error = errorFor(field);
          @let fieldId = ids.field + '-' + $index;
          @let hintId = fieldId + '-hint';
          @let errorId = fieldId + '-error';
          @let describedBy = describedByFor(field, hintId, errorId);

          @if (field.kind === 'text') {
              <div>
                <label [for]="fieldId" class="field-label">
                  {{ field.title || field.name }}
                  @if (field.required) {
                    <span aria-hidden="true" class="required-mark">*</span>
                    <span class="sr-only">(required)</span>
                  }
                </label>
                @if (field.description) {
                  <p [id]="hintId" class="field-hint">{{ field.description }}</p>
                }
                <input
                  [id]="fieldId"
                  class="field-input"
                  [type]="inputType(field)"
                  [formControl]="control"
                  [attr.aria-describedby]="describedBy"
                  [attr.aria-invalid]="!!error"
                  [attr.aria-required]="field.required"
                  [attr.minlength]="field.minLength ?? null"
                  [attr.maxlength]="field.maxLength ?? null"
                  autocomplete="off"
                  spellcheck="false"
                />
                @if (error) {
                  <p [id]="errorId" class="field-error">{{ error }}</p>
                }
              </div>
          } @else if (field.kind === 'number' || field.kind === 'integer') {
            <div>
              <label [for]="fieldId" class="field-label">
                {{ field.title || field.name }}
                @if (field.required) {
                  <span aria-hidden="true" class="required-mark">*</span>
                  <span class="sr-only">(required)</span>
                }
              </label>
              @if (field.description) {
                <p [id]="hintId" class="field-hint">{{ field.description }}</p>
              }
              <input
                [id]="fieldId"
                class="field-input max-w-48"
                type="number"
                inputmode="decimal"
                [formControl]="control"
                [attr.aria-describedby]="describedBy"
                [attr.aria-invalid]="!!error"
                [attr.aria-required]="field.required"
                [attr.min]="field.minimum ?? null"
                [attr.max]="field.maximum ?? null"
                [attr.step]="field.kind === 'integer' ? 1 : 'any'"
              />
              @if (error) {
                <p [id]="errorId" class="field-error">{{ error }}</p>
              }
            </div>
          } @else if (field.kind === 'boolean') {
              <fieldset [attr.aria-describedby]="describedBy">
                <legend class="field-label">
                  {{ field.title || field.name }}
                  @if (field.required) {
                    <span aria-hidden="true" class="required-mark">*</span>
                    <span class="sr-only">(required)</span>
                  }
                </legend>
                @if (field.description) {
                  <p [id]="hintId" class="field-hint">{{ field.description }}</p>
                }
                <div class="mt-2 flex flex-wrap gap-2" role="radiogroup" [attr.aria-label]="field.title || field.name">
                  <button
                    type="button"
                    class="chip"
                    role="radio"
                    [class.chip--on]="control.value === true"
                    [attr.aria-checked]="control.value === true"
                    [disabled]="resolving()"
                    (click)="pick(field, true)"
                  >
                    <span class="chip-mark chip-mark--radio" [class.chip-mark--on]="control.value === true" aria-hidden="true">
                      <ng-icon name="heroCheck" />
                    </span>
                    Yes
                  </button>
                  <button
                    type="button"
                    class="chip"
                    role="radio"
                    [class.chip--on]="control.value === false"
                    [attr.aria-checked]="control.value === false"
                    [disabled]="resolving()"
                    (click)="pick(field, false)"
                  >
                    <span class="chip-mark chip-mark--radio" [class.chip-mark--on]="control.value === false" aria-hidden="true">
                      <ng-icon name="heroCheck" />
                    </span>
                    No
                  </button>
                </div>
                @if (error) {
                  <p [id]="errorId" class="field-error">{{ error }}</p>
                }
              </fieldset>
          } @else {
              @if (choiceKind(field) === 'select') {
                <div>
                  <label [for]="fieldId" class="field-label">
                    {{ field.title || field.name }}
                    @if (field.required) {
                      <span aria-hidden="true" class="required-mark">*</span>
                      <span class="sr-only">(required)</span>
                    }
                  </label>
                  @if (field.description) {
                    <p [id]="hintId" class="field-hint">{{ field.description }}</p>
                  }
                  <select
                    [id]="fieldId"
                    class="field-input"
                    [formControl]="control"
                    [attr.aria-describedby]="describedBy"
                    [attr.aria-invalid]="!!error"
                    [attr.aria-required]="field.required"
                  >
                    <option [ngValue]="null">Choose…</option>
                    @for (option of field.options; track option.value) {
                      <option [ngValue]="option.value">{{ option.label }}</option>
                    }
                  </select>
                  @if (error) {
                    <p [id]="errorId" class="field-error">{{ error }}</p>
                  }
                </div>
              } @else {
                @let multi = choiceKind(field) === 'multi';
                <fieldset [attr.aria-describedby]="describedBy">
                  <legend class="field-label">
                    {{ field.title || field.name }}
                    @if (field.required) {
                      <span aria-hidden="true" class="required-mark">*</span>
                      <span class="sr-only">(required)</span>
                    }
                  </legend>
                  @if (field.description) {
                    <p [id]="hintId" class="field-hint">{{ field.description }}</p>
                  }
                  <div
                    class="mt-2 flex flex-wrap gap-2"
                    [attr.role]="multi ? 'group' : 'radiogroup'"
                    [attr.aria-label]="field.title || field.name"
                  >
                    @for (option of field.options; track option.value) {
                      @let on = isChosen(field, option.value);
                      <button
                        type="button"
                        class="chip"
                        [class.chip--on]="on"
                        [attr.role]="multi ? 'checkbox' : 'radio'"
                        [attr.aria-checked]="on"
                        [disabled]="resolving()"
                        (click)="choose(field, option.value)"
                      >
                        <span
                          class="chip-mark"
                          [class.chip-mark--radio]="!multi"
                          [class.chip-mark--on]="on"
                          aria-hidden="true"
                        >
                          <ng-icon name="heroCheck" />
                        </span>
                        {{ option.label }}
                      </button>
                    }
                  </div>
                  @if (error) {
                    <p [id]="errorId" class="field-error">{{ error }}</p>
                  }
                </fieldset>
              }
          }

        }

        @if (hasTextField()) {
          <p class="secrets-note flex items-start gap-2 text-xs/5 text-gray-700 dark:text-gray-200">
            <span class="mt-0.5 shrink-0 text-state-warning-700 dark:text-state-warning-400" aria-hidden="true">
              <ng-icon name="heroExclamationTriangle" />
            </span>
            Don't enter passwords or other secrets here.
          </p>
        }

        <footer class="flex items-center justify-between gap-3">
          <button type="button" class="ghost-btn" (click)="decline()" [disabled]="resolving()">
            Decline
          </button>
          <button type="submit" class="primary-btn" [disabled]="resolving() || invalid()">
            @if (resolving()) {
              <app-spinner size="sm" variant="on-solid" label="Sending" />
              <span>Sending…</span>
            } @else {
              <span>Submit</span>
            }
          </button>
        </footer>
      </form>
    </section>
  `,
  styles: `
    @reference "../../../../../../styles/theme.css";

    :host {
      display: block;
    }

    .elicit-card {
      animation: card-rise 0.36s cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* The global \`.message-block p\` rule adds 16px below prose paragraphs. */
    .elicit-card p {
      margin-bottom: 0;
    }

    .eyebrow {
      font-size: 0.6875rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--color-primary-accessible);
    }

    :host-context(.dark) .eyebrow {
      color: var(--color-primary-50);
    }

    .tool-name {
      font-size: 0.8125rem;
    }

    /* The server's own words, set off from the card's chrome so they read as
       quoted rather than as the app speaking. Plain text: never markdown. */
    .server-message {
      white-space: pre-line;
      overflow-wrap: anywhere;
      padding-left: 0.75rem;
      border-left: 2px solid var(--color-gray-300);
    }

    :host-context(.dark) .server-message {
      border-left-color: rgb(255 255 255 / 0.18);
    }

    fieldset {
      min-width: 0;
      border: 0;
      padding: 0;
      margin: 0;
    }

    .field-label {
      display: block;
      font-size: 0.875rem;
      line-height: 1.5rem;
      font-weight: 500;
      color: var(--color-gray-900);
    }

    :host-context(.dark) .field-label {
      color: var(--color-gray-50);
    }

    .required-mark {
      margin-left: 0.125rem;
      color: var(--color-state-danger-700);
    }

    :host-context(.dark) .required-mark {
      color: var(--color-state-danger-400);
    }

    .field-hint {
      margin-top: 0.125rem;
      font-size: 0.75rem;
      line-height: 1.25rem;
      color: var(--color-gray-600);
    }

    :host-context(.dark) .field-hint {
      color: var(--color-gray-300);
    }

    .field-input {
      display: block;
      width: 100%;
      margin-top: 0.5rem;
      border-radius: 0.75rem;
      padding: 0.5rem 0.75rem;
      font-size: 0.875rem;
      line-height: 1.5rem;
      color: var(--color-gray-900);
      background: var(--color-white);
      box-shadow: inset 0 0 0 1px var(--color-gray-300);
      border: 0;
    }

    .field-input:focus {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 1px;
    }

    .field-input:disabled {
      opacity: 0.55;
    }

    .field-input[aria-invalid='true'] {
      box-shadow: inset 0 0 0 1px var(--color-state-danger-600);
    }

    :host-context(.dark) .field-input {
      color: var(--color-gray-50);
      background: rgb(255 255 255 / 0.05);
      box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.14);
      color-scheme: dark;
    }

    :host-context(.dark) .field-input[aria-invalid='true'] {
      box-shadow: inset 0 0 0 1px var(--color-state-danger-400);
    }

    .field-error {
      margin-top: 0.375rem;
      font-size: 0.75rem;
      line-height: 1.25rem;
      color: var(--color-state-danger-700);
    }

    :host-context(.dark) .field-error {
      color: var(--color-state-danger-400);
    }

    /* ---- choice chips ---- */

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      border-radius: 0.875rem;
      padding: 0.5rem 0.875rem;
      font-size: 0.875rem;
      line-height: 1.25rem;
      font-weight: 500;
      color: var(--color-gray-900);
      background: var(--color-white);
      box-shadow: inset 0 0 0 1px var(--color-gray-200);
      transition:
        background-color 140ms ease,
        box-shadow 140ms ease;
    }

    .chip:hover:not(:disabled):not(.chip--on) {
      background: var(--color-gray-50);
      box-shadow: inset 0 0 0 1px var(--color-gray-300);
    }

    .chip:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .chip:disabled {
      opacity: 0.55;
      cursor: default;
    }

    /* Selection is a fill change plus a filled mark, never colour alone. */
    .chip--on {
      background: color-mix(in oklab, var(--color-secondary-500) 12%, var(--color-white));
      box-shadow: inset 0 0 0 1.5px color-mix(in oklab, var(--color-secondary-500) 60%, transparent);
    }

    :host-context(.dark) .chip {
      color: var(--color-gray-50);
      background: rgb(255 255 255 / 0.04);
      box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.08);
    }

    :host-context(.dark) .chip:hover:not(:disabled):not(.chip--on) {
      background: rgb(255 255 255 / 0.07);
      box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.14);
    }

    :host-context(.dark) .chip--on {
      background: color-mix(in oklab, var(--color-secondary-500) 22%, transparent);
      box-shadow: inset 0 0 0 1.5px color-mix(in oklab, var(--color-secondary-500) 60%, transparent);
    }

    .chip-mark {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1rem;
      height: 1rem;
      border-radius: 0.3125rem;
      box-shadow: inset 0 0 0 1.5px var(--color-gray-300);
      color: transparent;
      font-size: 0.625rem;
    }

    /* Single-select and Yes / No read as radios, multi-select as checkboxes,
       so selection never rests on the chip's fill alone. */
    .chip-mark--radio {
      border-radius: 9999px;
    }

    .chip-mark--on {
      background: var(--color-secondary-500);
      box-shadow: inset 0 0 0 1.5px var(--color-secondary-500);
      color: white;
    }

    :host-context(.dark) .chip-mark {
      box-shadow: inset 0 0 0 1.5px rgb(255 255 255 / 0.22);
    }

    :host-context(.dark) .chip-mark--on {
      background: var(--color-secondary-500);
      box-shadow: inset 0 0 0 1.5px var(--color-secondary-500);
      color: white;
    }

    /* ---- actions ---- */

    .primary-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      border-radius: 1rem;
      padding: 0.5rem 1rem;
      font-size: 0.8125rem;
      font-weight: 600;
      color: white;
      background: var(--color-secondary-500);
      transition:
        background-color 140ms ease,
        transform 140ms ease;
    }

    .primary-btn:hover:not(:disabled) {
      background: var(--color-secondary-600);
    }

    .primary-btn:active:not(:disabled) {
      transform: translateY(1px);
    }

    .primary-btn:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .primary-btn:disabled {
      opacity: 0.45;
      cursor: default;
    }

    .ghost-btn {
      border-radius: 1rem;
      padding: 0.5rem 0.875rem;
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--color-gray-600);
      background: transparent;
      transition:
        background-color 140ms ease,
        color 140ms ease;
    }

    .ghost-btn:hover:not(:disabled) {
      background: var(--color-gray-200);
      color: var(--color-gray-900);
    }

    .ghost-btn:focus-visible {
      outline: 2px solid var(--color-gray-400);
      outline-offset: 2px;
    }

    .ghost-btn:disabled {
      opacity: 0.5;
      cursor: default;
    }

    :host-context(.dark) .ghost-btn {
      color: var(--color-gray-300);
    }

    :host-context(.dark) .ghost-btn:hover:not(:disabled) {
      background: rgb(255 255 255 / 0.1);
      color: white;
    }

    @keyframes card-rise {
      from {
        opacity: 0;
        transform: translateY(8px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .elicit-card {
        animation: none;
      }
      .chip,
      .primary-btn,
      .ghost-btn {
        transition: none;
      }
    }
  `,
})
export class McpElicitationPromptComponent {
  request = input.required<McpElicitationRequest>();

  private readonly elicitation = inject(McpElicitationService);

  protected readonly resolving = signal(false);

  /** Set by Submit, so a field the user never touched shows why it blocks. */
  private readonly attempted = signal(false);

  /** Bumped on every value change; the error lines read it to re-evaluate. */
  private readonly revision = signal(0);

  protected readonly ids = (() => {
    const id = `mcp-elicitation-${nextFormId++}`;
    return { heading: `${id}-heading`, field: `${id}-field` };
  })();

  /** One reactive form per request; a new request (a new interrupt) gets a fresh one. */
  protected readonly form = computed(() => {
    const form = buildElicitationForm(this.request().fields);
    form.valueChanges.subscribe(() => this.revision.update((n) => n + 1));
    return form;
  });

  /** Submit stays disabled until every required field holds a valid answer (spec §5). */
  protected readonly invalid = computed(() => {
    this.revision();
    return this.form().invalid;
  });

  protected readonly serverName = computed(() => this.request().serverName || 'An MCP server');

  protected readonly tool = computed(() => toolLabel(this.request().toolName));

  protected readonly hasTextField = computed(() =>
    this.request().fields.some((f) => f.kind === 'text'),
  );

  protected inputType(field: McpElicitationField): string {
    return inputTypeFor(field);
  }

  protected choiceKind(field: McpElicitationField): 'chips' | 'select' | 'multi' {
    return choiceControl(field);
  }

  protected errorFor(field: McpElicitationField): string | null {
    this.revision();
    const control = this.form().controls[field.name];
    if (!control) return null;
    if (!this.attempted() && !control.touched) return null;
    return fieldError(field, control.errors);
  }

  protected describedByFor(field: McpElicitationField, hintId: string, errorId: string): string | null {
    const ids = [field.description ? hintId : null, this.errorFor(field) ? errorId : null].filter(
      (id): id is string => !!id,
    );
    return ids.length ? ids.join(' ') : null;
  }

  protected isChosen(field: McpElicitationField, value: string): boolean {
    this.revision();
    const current = this.form().controls[field.name]?.value;
    return Array.isArray(current) ? current.includes(value) : current === value;
  }

  /** Yes / No. A second click on the chosen answer clears an optional one. */
  protected pick(field: McpElicitationField, value: boolean): void {
    const control = this.form().controls[field.name];
    if (!control) return;
    const next = control.value === value && !field.required ? null : value;
    control.setValue(next);
    control.markAsTouched();
  }

  /** Multi-select toggles; single-select moves, and a second click clears an optional one. */
  protected choose(field: McpElicitationField, value: string): void {
    const control = this.form().controls[field.name];
    if (!control) return;
    let next: McpElicitationControlValue;
    if (field.multiSelect) {
      const current = Array.isArray(control.value) ? control.value : [];
      next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    } else {
      next = control.value === value && !field.required ? null : value;
    }
    control.setValue(next);
    control.markAsTouched();
  }

  protected async submit(): Promise<void> {
    if (this.resolving()) return;
    const form = this.form();
    if (form.invalid) {
      this.attempted.set(true);
      form.markAllAsTouched();
      this.revision.update((n) => n + 1);
      return;
    }
    this.resolving.set(true);
    try {
      const content = readElicitationAnswer(this.request().fields, form.getRawValue());
      await this.elicitation.resolve(this.request().interruptId, { action: 'accept', content });
    } finally {
      this.resolving.set(false);
    }
  }

  protected async decline(): Promise<void> {
    if (this.resolving()) return;
    this.resolving.set(true);
    try {
      await this.elicitation.decline(this.request().interruptId);
    } finally {
      this.resolving.set(false);
    }
  }
}
