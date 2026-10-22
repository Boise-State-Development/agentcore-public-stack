import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroCheck,
  heroChevronLeft,
  heroChevronRight,
  heroPencil,
} from '@ng-icons/heroicons/outline';
import {
  UserQuestionAnswer,
  UserQuestionRequest,
  UserQuestionService,
} from '../../../../../services/user-question/user-question.service';
import { SpinnerComponent } from '../../../../../components/spinner/spinner.component';

/**
 * Inline picker for the clarifying questions the agent paused its turn to ask.
 *
 * **Full width, soft, and unhurried** — deliberately NOT the visual language of
 * `ToolApprovalPromptComponent`, which it was first modelled on. That component
 * is a narrow pill with a hard 2px accent bar and hairline dividers, and it is
 * right for what it does: a two-button yes/no that should stay out of the way.
 * This one is a form the reader has to think about, so it takes the assistant
 * column's full width and trades the boxy chrome for rounded surfaces, roomy
 * hit areas and a calm ground. No border on the card at all — a soft ring and
 * a faint tint carry the edge instead.
 *
 * **One question at a time, with a pager.** Measured against real models, both
 * Haiku 4.5 and Sonnet 4.6 routinely ask three or four questions in a single
 * call, so rendering them stacked would drop a wall of radio groups into the
 * transcript.
 *
 * **The picker owns "Other" and "Skip".** The backend strips any model-supplied
 * lookalike (a supplied "Other" carries no free-text field, so selecting it
 * would record a bare string that teaches the model nothing), which is why they
 * are added here rather than rendered from `options`.
 *
 * Two house traps this component has already been bitten by — don't reintroduce
 * either when editing:
 * - Dark rules use `:host-context(.dark)`. Angular's emulated encapsulation
 *   stamps `[_ngcontent-…]` inside `:where()`, so `:where(.dark, .dark *)`
 *   compiles to `.dark[_ngcontent-…]` and silently never matches.
 * - Utility classes set directly on an `<ng-icon>` element do not apply. Put
 *   colour on the wrapper and let the icon inherit `currentColor`.
 */
@Component({
  selector: 'app-user-question-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, FormsModule, SpinnerComponent],
  providers: [
    provideIcons({
      heroArrowRight,
      heroCheck,
      heroChevronLeft,
      heroChevronRight,
      heroPencil,
    }),
  ],
  host: { class: 'block' },
  template: `
    <section
      class="question-card w-full rounded-2xl bg-gray-50/80 p-5 ring-1 ring-gray-200/70 dark:bg-white/[0.035] dark:ring-white/10"
      [attr.aria-label]="'Clarifying question ' + (index() + 1) + ' of ' + total()"
    >
      <!-- Heading: eyebrow + question. The pager sits with the eyebrow so the
           question line is never crowded by controls. -->
      <header class="mb-4 flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="eyebrow">{{ current().header }}</p>
          <h3 class="mt-2 text-sm/6 font-medium text-balance text-gray-900 dark:text-gray-50">
            {{ current().question }}
          </h3>
        </div>

        @if (total() > 1) {
          <div class="flex shrink-0 items-center gap-1 pt-0.5">
            <button
              type="button"
              class="pager-btn"
              (click)="back()"
              [disabled]="index() === 0 || resolving()"
              aria-label="Previous question"
            >
              <ng-icon name="heroChevronLeft" />
            </button>
            <span class="pager-count" aria-live="polite">
              {{ index() + 1 }} / {{ total() }}
            </span>
            <button
              type="button"
              class="pager-btn"
              (click)="next()"
              [disabled]="index() === total() - 1 || resolving()"
              aria-label="Next question"
            >
              <ng-icon name="heroChevronRight" />
            </button>
          </div>
        }
      </header>

      <!-- Options: discrete rounded rows with air between them, rather than a
           divided list. Selection reads from the tinted surface and the filled
           marker together, never from colour alone. -->
      <div
        class="grid gap-2"
        [attr.role]="current().multiSelect ? 'group' : 'radiogroup'"
        [attr.aria-label]="current().question"
      >
        @for (option of current().options; track option.label) {
          <button
            type="button"
            class="option"
            [class.option--on]="isSelected(option.label)"
            [attr.role]="current().multiSelect ? 'checkbox' : 'radio'"
            [attr.aria-checked]="isSelected(option.label)"
            [disabled]="resolving()"
            (click)="toggle(option.label)"
          >
            <span
              class="marker"
              [class.marker--multi]="current().multiSelect"
              [class.marker--on]="isSelected(option.label)"
              aria-hidden="true"
            >
              <ng-icon name="heroCheck" />
            </span>
            <span class="min-w-0 flex-1">
              <span class="block text-sm/6 font-medium text-gray-900 dark:text-gray-50">
                {{ option.label }}
              </span>
              @if (option.description) {
                <span class="mt-0.5 block text-xs/5 text-gray-600 dark:text-gray-300">
                  {{ option.description }}
                </span>
              }
            </span>
          </button>
        }

        <!-- "Other": always offered, never supplied by the model. Shares the
             option row's shape so it reads as one more choice. -->
        <label class="option option--other">
          <span class="marker marker--quiet" aria-hidden="true">
            <ng-icon name="heroPencil" />
          </span>
          <input
            type="text"
            class="other-input"
            placeholder="Something else…"
            [ngModel]="otherText()"
            (ngModelChange)="setOther($event)"
            [disabled]="resolving()"
            [attr.aria-label]="'Other answer for ' + current().header"
            (keydown.enter)="submitIfReady()"
          />
        </label>
      </div>

      <!-- Footer -->
      <footer class="mt-4 flex items-center justify-between gap-3">
        <button type="button" class="ghost-btn" (click)="skip()" [disabled]="resolving()">
          Skip
        </button>

        <div class="flex items-center gap-3">
          @if (answeredCount() > 0 && total() > 1) {
            <span class="text-xs tabular-nums text-gray-600 dark:text-gray-300">
              {{ answeredCount() }} of {{ total() }} answered
            </span>
          }
          @if (index() < total() - 1) {
            <button type="button" class="primary-btn" (click)="next()" [disabled]="resolving()">
              <span>Next</span>
              <ng-icon name="heroArrowRight" />
            </button>
          } @else {
            <button
              type="button"
              class="primary-btn"
              (click)="submit()"
              [disabled]="resolving() || answeredCount() === 0"
            >
              @if (resolving()) {
                <app-spinner size="sm" variant="on-solid" label="Working" />
                <span>Working…</span>
              } @else {
                <span>Submit</span>
              }
            </button>
          }
        </div>
      </footer>
    </section>
  `,
  styles: `
    @reference "../../../../../../styles/theme.css";

    :host {
      display: block;
    }

    .question-card {
      animation: card-rise 0.36s cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* The global \`.message-block p\` rule adds 16px below prose paragraphs;
       inside the card the eyebrow is a label, not a paragraph. */
    .question-card p {
      margin-bottom: 0;
    }

    /* Deliberately unadorned. An icon here would be pure decoration — the
       card already announces itself by shape and position, and a little
       glyph on an AI prompt is the first thing that makes it look generated. */
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

    /* ---- options ---- */

    .option {
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
      width: 100%;
      padding: 0.75rem 0.875rem;
      border-radius: 0.875rem;
      text-align: left;
      background: var(--color-white);
      box-shadow: inset 0 0 0 1px var(--color-gray-200);
      transition:
        background-color 140ms ease,
        box-shadow 140ms ease;
    }

    .option:hover:not(:disabled):not(.option--on) {
      background: var(--color-gray-50);
      box-shadow: inset 0 0 0 1px var(--color-gray-300);
    }

    .option:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    /* Only the "Other" row needs this: it is a <label>, so the thing that
       actually takes focus is the input inside it. Applying it to every
       option also matched a plain MOUSE click on a button (:focus-within is
       true after one), stacking an outline outside the row. */
    .option--other:focus-within {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .option:disabled {
      opacity: 0.55;
      cursor: default;
    }

    /* Selection is a fill change, never an added stroke. Every row carries
       exactly one hairline whatever its state, so a selected row cannot read
       as a double stroke — which is what stacking a coloured ring under the
       focus outline produced. The warm tint and the filled marker carry the
       state together, so it is never colour alone. */
    .option--on {
      background: color-mix(in oklab, var(--color-secondary-500) 10%, var(--color-white));
      box-shadow: inset 0 0 0 1px
        color-mix(in oklab, var(--color-secondary-500) 38%, transparent);
    }

    .option--other {
      cursor: text;
    }

    :host-context(.dark) .option {
      background: rgb(255 255 255 / 0.04);
      box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.08);
    }

    :host-context(.dark) .option:hover:not(:disabled):not(.option--on) {
      background: rgb(255 255 255 / 0.07);
      box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.14);
    }

    :host-context(.dark) .option--on {
      background: color-mix(in oklab, var(--color-secondary-500) 20%, transparent);
      box-shadow: inset 0 0 0 1px
        color-mix(in oklab, var(--color-secondary-500) 52%, transparent);
    }

    /* ---- selection marker ---- */

    .marker {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      width: 1.25rem;
      height: 1.25rem;
      margin-top: 0.1875rem;
      border-radius: 9999px;
      background: transparent;
      box-shadow: inset 0 0 0 1.5px var(--color-gray-300);
      color: transparent;
      font-size: 0.75rem;
      transition:
        background-color 140ms ease,
        box-shadow 140ms ease,
        color 140ms ease;
    }

    /* Multi-select reads as a checkbox, single-select as a radio. */
    .marker--multi {
      border-radius: 0.4375rem;
    }

    .marker--on {
      background: var(--color-secondary-500);
      box-shadow: inset 0 0 0 1.5px var(--color-secondary-500);
      color: white;
    }

    .marker--quiet {
      color: var(--color-gray-500);
      box-shadow: inset 0 0 0 1.5px transparent;
    }

    :host-context(.dark) .marker {
      box-shadow: inset 0 0 0 1.5px rgb(255 255 255 / 0.22);
    }

    :host-context(.dark) .marker--on {
      background: var(--color-secondary-500);
      box-shadow: inset 0 0 0 1.5px var(--color-secondary-500);
      color: white;
    }

    :host-context(.dark) .marker--quiet {
      color: var(--color-gray-400);
      box-shadow: inset 0 0 0 1.5px transparent;
    }

    /* ---- other ---- */

    .other-input {
      flex: 1 1 auto;
      min-width: 0;
      align-self: center;
      background: transparent;
      border: 0;
      padding: 0;
      font-size: 0.875rem;
      line-height: 1.5rem;
      color: var(--color-gray-900);
    }

    .other-input::placeholder {
      color: var(--color-gray-500);
    }

    .other-input:focus {
      outline: none;
    }

    :host-context(.dark) .other-input {
      color: var(--color-gray-50);
    }

    :host-context(.dark) .other-input::placeholder {
      color: var(--color-gray-400);
    }

    /* ---- pager ---- */

    .pager-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1.75rem;
      height: 1.75rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      color: var(--color-gray-600);
      transition:
        background-color 140ms ease,
        color 140ms ease;
    }

    .pager-btn:hover:not(:disabled) {
      background: var(--color-gray-200);
      color: var(--color-gray-900);
    }

    .pager-btn:disabled {
      opacity: 0.3;
      cursor: default;
    }

    .pager-btn:focus-visible {
      outline: 2px solid var(--color-secondary-500);
      outline-offset: 2px;
    }

    .pager-count {
      font-size: 0.75rem;
      font-variant-numeric: tabular-nums;
      color: var(--color-gray-600);
    }

    :host-context(.dark) .pager-btn {
      color: var(--color-gray-300);
    }

    :host-context(.dark) .pager-btn:hover:not(:disabled) {
      background: rgb(255 255 255 / 0.1);
      color: white;
    }

    :host-context(.dark) .pager-count {
      color: var(--color-gray-300);
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
      .question-card {
        animation: none;
      }
      .option,
      .marker,
      .primary-btn,
      .ghost-btn,
      .pager-btn {
        transition: none;
      }
    }
  `,
})
export class UserQuestionPromptComponent {
  request = input.required<UserQuestionRequest>();

  private questionService = inject(UserQuestionService);

  protected index = signal(0);
  protected resolving = signal(false);

  /** Answers accumulated across the pager, keyed by question header. */
  private readonly answers = signal<Record<string, UserQuestionAnswer>>({});

  protected total = computed<number>(() => this.request().questions.length);

  protected current = computed(() => {
    const questions = this.request().questions;
    // Clamp rather than index blindly: `request` is an input and could in
    // principle be swapped for a shorter set while the pager sits past its end.
    return questions[Math.min(this.index(), questions.length - 1)];
  });

  protected answeredCount = computed<number>(
    () =>
      Object.values(this.answers()).filter(
        (a) => a.selected.length > 0 || !!a.text?.trim(),
      ).length,
  );

  protected otherText = computed<string>(
    () => this.answers()[this.current().header]?.text ?? '',
  );

  protected isSelected(label: string): boolean {
    return !!this.answers()[this.current().header]?.selected.includes(label);
  }

  /**
   * Select an option. Multi-select toggles; single-select replaces, so a
   * second click on a different option moves the choice rather than adding to
   * it — and a second click on the SAME option clears it, which is the only
   * way to undo a misclick on a question the user would rather leave blank.
   */
  protected toggle(label: string): void {
    const header = this.current().header;
    const multi = this.current().multiSelect;
    this.answers.update((all) => {
      const existing = all[header] ?? { selected: [] };
      const has = existing.selected.includes(label);
      const selected = multi
        ? has
          ? existing.selected.filter((l) => l !== label)
          : [...existing.selected, label]
        : has
          ? []
          : [label];
      return { ...all, [header]: { ...existing, selected } };
    });
  }

  protected setOther(text: string): void {
    const header = this.current().header;
    this.answers.update((all) => {
      const existing = all[header] ?? { selected: [] };
      return { ...all, [header]: { ...existing, text } };
    });
  }

  protected back(): void {
    this.index.update((i) => Math.max(0, i - 1));
  }

  protected next(): void {
    this.index.update((i) => Math.min(this.total() - 1, i + 1));
  }

  /** Enter in the "Other" field advances, or submits on the last question. */
  protected submitIfReady(): void {
    if (this.index() < this.total() - 1) {
      this.next();
      return;
    }
    if (this.answeredCount() > 0) {
      void this.submit();
    }
  }

  protected async submit(): Promise<void> {
    if (this.resolving()) return;
    this.resolving.set(true);
    try {
      // Only answered questions are sent. The backend marks the rest skipped,
      // which is the honest record: the user chose not to answer them, and
      // inventing a default here would put words in their mouth.
      const answers: Record<string, UserQuestionAnswer> = {};
      for (const [header, answer] of Object.entries(this.answers())) {
        const text = answer.text?.trim();
        if (answer.selected.length === 0 && !text) continue;
        answers[header] = text
          ? { selected: answer.selected, text }
          : { selected: answer.selected };
      }
      await this.questionService.resolve(this.request().interruptId, { answers });
    } finally {
      this.resolving.set(false);
    }
  }

  protected async skip(): Promise<void> {
    if (this.resolving()) return;
    this.resolving.set(true);
    try {
      await this.questionService.skip(this.request().interruptId);
    } finally {
      this.resolving.set(false);
    }
  }
}
