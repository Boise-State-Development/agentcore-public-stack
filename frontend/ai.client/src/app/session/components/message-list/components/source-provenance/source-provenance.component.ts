import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroExclamationTriangle,
  heroDocumentText,
  heroQuestionMarkCircle,
} from '@ng-icons/heroicons/outline';
import type { SourceProvenanceClaim, SourceLocator } from '../../../../../shared/utils/stream-parser';

/**
 * What a `verify_sources` call found, rendered under the answer it belongs to
 * (`docs/specs/source-provenance-verification.md` §4).
 *
 * The design rule the spec insists on, and the reason this is not a row in the
 * collapsed tool rail: **a plain page reference is only ever shown for
 * `supported`**. Every other state says, in its own words, that it is not a
 * confirmed citation. `quote_unsupported` is the hallucination signal — the
 * passage is real and the value is not in it — so it renders as a warning and
 * is never collapsed out of sight. The ported design's own history is the
 * argument: its badge keyed on "did we find the quote", which gave a
 * fabricated amount the same confident chip as a correct one.
 *
 * Disclosure follows from that. A run where everything checked out is one
 * quiet line, because a clean result should not shout; a run with anything
 * unconfirmed opens itself and stays open.
 *
 * Colour is never the only signal: each state carries an icon and words. The
 * states are not a severity ramp either — `unassessed` and `unverified` are
 * absences of evidence, not failures, and are styled as neutral rather than as
 * lesser warnings, so the single red thing on screen is the one that earned it.
 */
@Component({
  selector: 'app-source-provenance',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCheckCircle,
      heroExclamationTriangle,
      heroDocumentText,
      heroQuestionMarkCircle,
    }),
  ],
  styles: `
    :host {
      display: block;
    }
  `,
  template: `
    @if (claims().length) {
      <div
        class="mt-2 w-full rounded-lg border text-[13px]"
        [class]="unconfirmed().length ? warningShell : calmShell"
      >
        <!-- Header: the one-line verdict, and the toggle when there is detail. -->
        <button
          type="button"
          class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
          [attr.aria-expanded]="isOpen()"
          [attr.aria-controls]="panelId"
          (click)="toggle()"
        >
          <ng-icon
            [name]="unconfirmed().length ? 'heroExclamationTriangle' : 'heroCheckCircle'"
            class="shrink-0 text-base"
            [class]="unconfirmed().length ? 'text-state-warning-600 dark:text-state-warning-400' : 'text-state-success-600 dark:text-state-success-400'"
            aria-hidden="true"
          />
          <span class="min-w-0 flex-1">
            <span class="font-medium">{{ headline() }}</span>
            @if (filename()) {
              <span class="text-gray-500 dark:text-gray-400"> in {{ filename() }}</span>
            }
          </span>
          <span class="shrink-0 text-xs text-gray-500 dark:text-gray-400">
            {{ isOpen() ? 'Hide' : 'Details' }}
          </span>
        </button>

        @if (isOpen()) {
          <ul [id]="panelId" class="list-none space-y-2 border-t px-3 py-2.5 border-gray-200 dark:border-gray-700">
            @for (claim of claims(); track claim.id + claim.quote) {
              <li class="flex gap-2.5">
                <ng-icon
                  [name]="iconFor(claim.state)"
                  class="mt-0.5 shrink-0 text-sm"
                  [class]="iconClassFor(claim.state)"
                  aria-hidden="true"
                />
                <div class="min-w-0 flex-1">
                  <p class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span class="font-medium break-words">{{ label(claim) }}</span>
                    <!--
                      The locator sits beside the verdict, never alone: on its
                      own, "p. 1" reads as a confirmed citation whatever the
                      state next to it says.
                    -->
                    <span class="text-xs" [class]="verdictClassFor(claim.state)">{{ verdict(claim) }}</span>
                  </p>
                  @if (claim.quote) {
                    <p class="mt-0.5 text-gray-600 dark:text-gray-400 break-words">
                      <q class="italic">{{ claim.quote }}</q>
                    </p>
                  }
                </div>
              </li>
            }
          </ul>
        }
      </div>
    }
  `,
})
export class SourceProvenanceComponent {
  claims = input.required<readonly SourceProvenanceClaim[]>();

  protected readonly panelId = `provenance-${Math.random().toString(36).slice(2, 9)}`;

  protected readonly calmShell =
    'border-gray-200 bg-gray-50 text-gray-800 dark:border-gray-700 dark:bg-gray-800/50 dark:text-gray-200';
  protected readonly warningShell =
    'border-state-warning-300 bg-state-warning-50 text-state-warning-900 dark:border-state-warning-700/60 dark:bg-state-warning-950/30 dark:text-state-warning-100';

  /** Claims whose value the document does not support. */
  protected readonly unconfirmed = computed(() =>
    this.claims().filter(c => c.state === 'quote_unsupported'),
  );

  /**
   * Open by default whenever something is unconfirmed.
   *
   * A warning the reader has to click to discover is not a warning, and this
   * is the state the whole feature exists to surface.
   */
  private readonly opened = signal<boolean | null>(null);
  protected readonly isOpen = computed(
    () => this.opened() ?? this.unconfirmed().length > 0,
  );

  protected toggle(): void {
    this.opened.set(!this.isOpen());
  }

  /** The document, when every claim came from the same one. */
  protected readonly filename = computed(() => {
    const names = new Set(this.claims().map(c => c.filename).filter(Boolean));
    return names.size === 1 ? [...names][0] : '';
  });

  protected readonly headline = computed(() => {
    const total = this.claims().length;
    const bad = this.unconfirmed().length;
    if (bad) {
      return bad === 1
        ? '1 value could not be confirmed'
        : `${bad} values could not be confirmed`;
    }
    const supported = this.claims().filter(c => c.state === 'supported').length;
    if (supported === total) {
      return total === 1 ? '1 value checked against the source' : `${total} values checked against the source`;
    }
    // Some were traced but not confirmable. Say so rather than implying the
    // whole set was verified.
    return `${supported} of ${total} values confirmed`;
  });

  /** What the claim is about: the value as stated, else the model's own label. */
  protected label(claim: SourceProvenanceClaim): string {
    return claim.value?.trim() || claim.id || 'This value';
  }

  /**
   * The words beside each claim. These carry the meaning — the colour and icon
   * only reinforce them — so each one says what is and is not established.
   */
  protected verdict(claim: SourceProvenanceClaim): string {
    switch (claim.state) {
      case 'supported':
        return `found on ${this.locator(claim.locator)}`;
      case 'quote_unsupported':
        return `not in the quoted passage on ${this.locator(claim.locator)}`;
      case 'unassessed':
        return `passage found on ${this.locator(claim.locator)}, value not checked`;
      default:
        return 'no matching passage in the document';
    }
  }

  /**
   * A locator in words. An `estimated` position is hedged with "about",
   * because a position the data cannot support must not read as exact.
   */
  protected locator(loc: SourceLocator | null): string {
    if (!loc) return 'an unknown position';
    const about = loc.precision === 'estimated' ? 'about ' : '';
    const unit =
      loc.kind === 'page' ? 'page' : loc.kind === 'paragraph' ? 'paragraph' : 'line';
    return `${about}${unit} ${loc.value}`;
  }

  protected iconFor(state: SourceProvenanceClaim['state']): string {
    if (state === 'supported') return 'heroCheckCircle';
    if (state === 'quote_unsupported') return 'heroExclamationTriangle';
    if (state === 'unassessed') return 'heroDocumentText';
    return 'heroQuestionMarkCircle';
  }

  protected iconClassFor(state: SourceProvenanceClaim['state']): string {
    if (state === 'supported') return 'text-state-success-600 dark:text-state-success-400';
    if (state === 'quote_unsupported') return 'text-state-warning-600 dark:text-state-warning-400';
    return 'text-gray-400 dark:text-gray-500';
  }

  protected verdictClassFor(state: SourceProvenanceClaim['state']): string {
    if (state === 'quote_unsupported') {
      return 'font-medium text-state-warning-700 dark:text-state-warning-300';
    }
    return 'text-gray-500 dark:text-gray-400';
  }
}
