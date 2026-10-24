import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import type { SourceProvenanceClaim, SourceLocator } from '../../../../../shared/utils/stream-parser';

/**
 * What a `verify_sources` call found, rendered under the answer it belongs to
 * (`docs/specs/source-provenance-verification.md` §4).
 *
 * Styled as a sibling of the tool rail — same chevron, same muted one-line
 * header, same bordered row list, same status dots — because it is the same
 * kind of thing: a compact, scannable record of what the agent established,
 * folded away until someone checks it. It is a separate block rather than a
 * rail row because the rail says what the agent *did*, while this says whether
 * the answer's values are *in the source*, and that belongs with the answer.
 *
 * Two rules survive the restyle, because they are the feature rather than its
 * appearance:
 *
 * 1. **A plain locator appears only for `supported`.** Every other state says
 *    in words what is and is not established, and the locator never stands
 *    alone — on its own, "page 1" reads as a confirmed citation whatever sits
 *    beside it.
 * 2. **Anything unconfirmed opens the list by default.** A warning behind a
 *    click is not a warning, and `quote_unsupported` — the passage is real,
 *    the value is not in it — is the state the whole feature exists to
 *    surface. The header keeps saying so even when the list is collapsed.
 *
 * Colour is never the only signal: the header counts unconfirmed values in
 * words, and each row pairs its dot with a sentence.
 */
@Component({
  selector: 'app-source-provenance',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
    }

    /* Chevron, dot and accordion match tool-rail.component.css. Duplicated
       rather than shared because Angular scopes component styles, and a rail
       that drifted from this panel would read as two unrelated widgets. */
    .chevron {
      transition: transform 0.2s ease-out;
    }

    .chevron.open {
      transform: rotate(90deg);
    }

    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 9999px;
      flex-shrink: 0;
    }

    .collapsible-content {
      display: grid;
      grid-template-rows: 0fr;
      transition: grid-template-rows 0.2s ease-out;
    }

    .collapsible-content.open {
      grid-template-rows: 1fr;
    }

    .collapsible-content > div {
      overflow: hidden;
      opacity: 0;
      transition: opacity 0s ease-out;
    }

    .collapsible-content.open > div {
      opacity: 1;
      transition: opacity 0.15s ease-out 0.05s;
    }

    /* The quote renders inside '.message-block', whose global prose rule gives
       every <p> a 16px bottom margin — which would punch a gap into a dense
       accordion row. Same fix the rail's batch summary needs. */
    .claim-quote {
      margin: 0.125rem 0 0;
    }

    /* The message block's global prose rules (styles.css, unlayered) restyle
       every ul/li — list markers and padding — and beat any Tailwind utility,
       the same way they beat the rail's <pre> styling. Without this each row
       carries a bullet next to its status dot. */
    .claim-list {
      list-style: none;
      margin: 0.5rem 0 0;
      padding-left: 0;
    }

    .claim-list > li {
      list-style: none;
      margin: 0;
      padding-left: 0.625rem;
    }

    .claim-list > li::marker {
      content: '';
    }
  `,
  template: `
    @if (claims().length) {
      <div class="mt-2">
        <button
          type="button"
          (click)="toggle()"
          class="group/prov flex w-full cursor-pointer items-center gap-2 text-left"
          [attr.aria-expanded]="isOpen()"
          [attr.aria-controls]="panelId"
          [attr.aria-label]="isOpen() ? 'Hide source details' : 'Show source details'"
        >
          <svg
            class="chevron size-3 shrink-0 text-gray-400 dark:text-gray-500"
            [class.open]="isOpen()"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>

          <span
            class="min-w-0 truncate text-xs text-gray-500 group-hover/prov:text-gray-700 dark:text-gray-400 dark:group-hover/prov:text-gray-200"
          >
            {{ headline() }}
          </span>

          <!-- The count of values the document does not support, in the same
               place and the same weight the rail puts "N failed". It stays
               visible when the list is collapsed: the detail folds away, the
               fact that something is unconfirmed does not. -->
          @if (unconfirmed().length; as bad) {
            <span
              class="shrink-0 text-[11px] font-medium text-state-danger-600 dark:text-state-danger-400"
            >
              {{ bad }} unconfirmed
            </span>
          }
        </button>

        <!-- CSS-collapsed for the rail's accordion feel, but hidden from
             assistive tech and from tab order while closed. The rows carry
             passages from the user's own document; clipping them to zero
             height still leaves them reachable by a screen reader, which
             would read out quotes nobody opened. -->
        <div
          class="collapsible-content"
          [class.open]="isOpen()"
          [attr.aria-hidden]="isOpen() ? null : 'true'"
          [attr.inert]="isOpen() ? null : ''"
        >
          <div>
            <ul
              [id]="panelId"
              class="claim-list divide-y divide-gray-200/80 overflow-hidden rounded-lg border border-gray-200/80 dark:divide-white/10 dark:border-white/10"
            >
              @for (claim of claims(); track claim.id + claim.quote) {
                <li class="bg-white/40 py-1.5 pr-2.5 dark:bg-transparent">
                  <div class="flex items-center gap-2">
                    <span [class]="dotClass(claim.state)"></span>
                    <span class="min-w-0 truncate text-xs text-gray-600 dark:text-gray-300">
                      {{ label(claim) }}
                    </span>
                    <!-- The verdict sits with the value, never a bare locator:
                         "page 1" alone reads as a confirmed citation. -->
                    <span class="truncate text-[11px]" [class]="verdictClass(claim.state)">
                      {{ verdict(claim) }}
                    </span>
                  </div>
                  @if (claim.quote) {
                    <p class="claim-quote pl-[14px] text-[11px] text-gray-400 dark:text-gray-500">
                      <q>{{ claim.quote }}</q>
                    </p>
                  }
                </li>
              }
            </ul>
          </div>
        </div>
      </div>
    }
  `,
})
export class SourceProvenanceComponent {
  claims = input.required<readonly SourceProvenanceClaim[]>();

  protected readonly panelId = `provenance-${Math.random().toString(36).slice(2, 9)}`;

  /** Claims whose value the document does not support. */
  protected readonly unconfirmed = computed(() =>
    this.claims().filter(c => c.state === 'quote_unsupported'),
  );

  /**
   * Open by default whenever something is unconfirmed; collapsed otherwise,
   * like the rail. A warning the reader has to click to discover is not a
   * warning, and this is the state the whole feature exists to surface.
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
    const supported = this.claims().filter(c => c.state === 'supported').length;
    const doc = this.filename() ? ` in ${this.filename()}` : '';
    const values = total === 1 ? '1 value' : `${total} values`;
    if (this.unconfirmed().length) {
      return `Checked ${values}${doc}`;
    }
    if (supported === total) {
      return `Confirmed ${values}${doc}`;
    }
    // Some were traced but not confirmable. Say so rather than implying the
    // whole set was verified.
    return `Confirmed ${supported} of ${total} values${doc}`;
  });

  /** What the claim is about: the value as stated, else the model's own label. */
  protected label(claim: SourceProvenanceClaim): string {
    return claim.value?.trim() || claim.id || 'This value';
  }

  /**
   * The words beside each claim. These carry the meaning — the dot only
   * reinforces them — so each says what is and is not established.
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

  /** Same vocabulary as the rail's status dots. */
  protected dotClass(state: SourceProvenanceClaim['state']): string {
    if (state === 'supported') return 'status-dot bg-state-success-500';
    if (state === 'quote_unsupported') return 'status-dot bg-state-danger-500';
    return 'status-dot bg-gray-400';
  }

  protected verdictClass(state: SourceProvenanceClaim['state']): string {
    if (state === 'quote_unsupported') {
      return 'font-medium text-state-danger-600 dark:text-state-danger-400';
    }
    return 'text-gray-400 dark:text-gray-500';
  }
}
