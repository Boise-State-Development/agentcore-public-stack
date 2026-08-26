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
 *
 * `part` splits the two halves for a layout that needs them in different places — My
 * Agents' list view puts the badge inline beside the name and the note on its own line
 * below. It is a *split*, never a filter: no caller may render `'badge'` and drop the
 * note, because that turns a layout toggle into a way to hide the one sentence telling
 * an author why their submission came back.
 */
export type ListingStatusPart = 'all' | 'badge' | 'note';

@Component({
  selector: 'app-listing-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (listing(); as l) {
      @if (showBadge()) {
        <span
          class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-medium"
          [class]="badgeClass()"
        >
          {{ badgeLabel() }}
        </span>
      }

      @if (showNote()) {
        @if (stillLive()) {
          <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
            {{ stillLive() }}
          </p>
        }

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
    }
  `,
})
export class ListingStatusComponent {
  readonly listing = input.required<AgentListingBlock | undefined>();

  /** Which half to render. `'all'` — the default — is the stacked card treatment. */
  readonly part = input<ListingStatusPart>('all');

  readonly showBadge = computed(() => this.part() !== 'note');
  readonly showNote = computed(() => this.part() !== 'badge');

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
   * The sentence that stops the badge from lying, or `null`.
   *
   * "In review" and "Changes requested" both describe a *submission*, and an author whose
   * agent is in the store reads them as a statement about their listing — that it came
   * down, or is about to. Neither is true: submitting an update leaves the approved version
   * serving, and requesting changes on a live listing deliberately does not unpublish it.
   * The pointer is what tells them apart, which is why `publishedVersion` is on the wire.
   */
  readonly stillLive = computed(() => {
    const l = this.listing();
    if (!l?.publishedVersion) return null;
    if (l.state === 'in_review') {
      return 'Your published version stays in the store, unchanged, until this update is approved.';
    }
    if (l.state === 'changes_requested') {
      return 'Your published version is still in the store while you make these changes.';
    }
    return null;
  });

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
      // Its own heading, not "changes requested": the reviewer is not asking for a fix,
      // and a note headed as a change request would be read as a to-do list.
      case 'rejected':
        return 'Why this was declined';
      case 'published':
        return 'Note from the reviewer';
      default:
        return 'Note on this listing';
    }
  });

  readonly noteClass = computed(() => {
    const state = this.listing()?.state;
    return state === 'changes_requested' || state === 'taken_down' || state === 'rejected'
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
