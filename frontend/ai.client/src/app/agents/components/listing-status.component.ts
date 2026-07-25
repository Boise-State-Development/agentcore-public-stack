import { Component, ChangeDetectionStrategy, computed, input } from '@angular/core';
import {
  AdminEdit,
  AgentListingBlock,
  LISTING_STATE_CLASSES,
  LISTING_STATE_LABELS,
} from '../models/store.model';
import { formatShortDate } from '../../shared/utils/iso-date';

/**
 * The author's view of their own listing: the state, the reviewer's reason, and what
 * an admin changed (D2, D13).
 *
 * Everything here exists so the author never has to ask what happened. "Changes
 * requested" without the reason is worse than no badge at all, and D13 is explicit
 * that admin edits to presentation are shown rather than made quietly.
 */
@Component({
  selector: 'app-listing-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (listing(); as l) {
      <span
        class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-medium"
        [class]="badgeClass()"
      >
        {{ badgeLabel() }}
      </span>

      @if (note(); as text) {
        <div
          class="mt-2 rounded-2xl border px-3 py-2 text-xs/5"
          [class]="noteClass()"
        >
          <p class="font-medium">{{ noteHeading() }}</p>
          <p class="mt-0.5 whitespace-pre-line">{{ text }}</p>
        </div>
      }

      @if (edits().length) {
        <ul class="mt-2 space-y-0.5">
          @for (edit of edits(); track edit.field + edit.at) {
            <li class="text-xs/5 text-gray-500 dark:text-gray-400">
              An admin updated the {{ edit.field }}@if (editedOn(edit); as on) {
                <span> on {{ on }}</span>
              }
            </li>
          }
        </ul>
      }
    }
  `,
})
export class ListingStatusComponent {
  readonly listing = input.required<AgentListingBlock | undefined>();

  readonly badgeLabel = computed(() => {
    const state = this.listing()?.state;
    return state ? LISTING_STATE_LABELS[state] : '';
  });

  readonly badgeClass = computed(() => {
    const state = this.listing()?.state;
    return state ? LISTING_STATE_CLASSES[state] : '';
  });

  readonly note = computed(() => this.listing()?.reviewNote?.trim() || null);

  /**
   * Who wrote the note depends on the state, and mislabelling it is worse than not
   * saying. One field carries both voices: submission writes the author's note to the
   * reviewer into `reviewNote`, and a review overwrites it with the reviewer's reason.
   *
   * Only the states a reviewer must have driven are attributed to one. `private` is
   * the ambiguous case — withdrawing from `in_review` leaves the author's own note
   * behind, withdrawing from `changes_requested` leaves the reviewer's — so it stays
   * neutral rather than guessing.
   */
  readonly noteHeading = computed(() => {
    switch (this.listing()?.state) {
      case 'in_review':
        return 'Note on this submission';
      case 'changes_requested':
        return 'The reviewer asked for changes';
      case 'taken_down':
        return 'Why this was taken down';
      case 'published':
        return 'Note from the reviewer';
      default:
        return 'Note on this listing';
    }
  });

  readonly noteClass = computed(() => {
    const state = this.listing()?.state;
    return state === 'changes_requested' || state === 'taken_down'
      ? 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-200'
      : 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300';
  });

  /**
   * The most recent edit per field, newest first.
   *
   * The log is append-only, so an admin who fixes a tagline three times produces three
   * rows saying the same thing. Collapsing to the latest per field keeps the card
   * readable while still naming everything that was touched.
   */
  readonly edits = computed<AdminEdit[]>(() => {
    const all = this.listing()?.adminEdits ?? [];
    const latest = new Map<string, AdminEdit>();
    for (const edit of all) {
      const seen = latest.get(edit.field);
      if (!seen || (edit.at ?? '') > (seen.at ?? '')) {
        latest.set(edit.field, edit);
      }
    }
    return [...latest.values()].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));
  });

  editedOn(edit: AdminEdit): string {
    return formatShortDate(edit.at);
  }
}
