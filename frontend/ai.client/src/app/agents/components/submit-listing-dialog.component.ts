import { Component, ChangeDetectionStrategy, OnInit, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroExclamationTriangle, heroEye, heroEyeSlash } from '@ng-icons/heroicons/outline';
import { AgentListingService } from '../services/agent-listing.service';
import { ListingReachability } from '../models/reachability';
import {
  AgentCategory,
  AgentListingBlock,
  SkillExposure,
  TAGLINE_MAX,
  deriveTagline,
} from '../models/store.model';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

export interface SubmitListingDialogData {
  agentId: string;
  agentName: string;
  /** Present on a resubmission; its category preselects the picker. */
  listing?: AgentListingBlock;
  /** The current listing subtitle, if the Agent already has one. */
  tagline?: string;
  /** Only used to prefill an absent tagline — see `deriveTagline` (#749). */
  description?: string;
}

/** The listing after submission, or `undefined` if cancelled. */
export type SubmitListingDialogResult = AgentListingBlock | undefined;

/**
 * Submit an Agent to the marketplace (D2), with the D7 disclosures.
 *
 * The dialog does not decide anything. It asks `GET /agents/{id}/listing/preflight`,
 * which runs the same checks the transition enforces, and renders the answers:
 *
 * * **Skill exposure (D7.1)** — publishing an Agent effectively publishes the contents
 *   of every skill its author wrote and bound, because Skills v2 resolves a `skill`
 *   binding on `skill.owner_id == agent.owner_id`. The names are listed, not counted.
 * * **Memory spaces (D7.2)** — a `memory_space` binding blocks submission outright, so
 *   the form is hidden and the backend's message (which names the space) explains why.
 *   The author learns this before filling in a category, not after clicking.
 * * **Going public** — the marketplace is public-only, and every Agent starts PRIVATE,
 *   so most first submissions need visibility widened. That is a *checkbox here*, not a
 *   block: `makePublic` rides the submit request and the backend widens in the same
 *   write. Sending the author to the agent editor to change a setting and come back was
 *   the whole reason this needed fixing — but it stays consent, so the box starts
 *   unticked and Submit is disabled until it is ticked.
 */
@Component({
  selector: 'app-submit-listing-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroXMark, heroExclamationTriangle, heroEye, heroEyeSlash })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative flex max-h-[90vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="submit-listing-title"
        aria-describedby="submit-listing-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="submit-listing-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              @if (isUpdate()) {
                Submit an update
              } @else {
                {{ isResubmission() ? 'Submit again for review' : 'Submit to the marketplace' }}
              }
            </h2>
            <p id="submit-listing-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              @if (isUpdate()) {
                An admin reviews your changes before they reach anyone. The version of
                <span class="font-medium">{{ data.agentName }}</span> in the store stays live and
                unchanged until they approve.
              } @else {
                An admin reviews <span class="font-medium">{{ data.agentName }}</span> before it
                appears in the store. You'll see their decision here.
              }
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-6 py-4">
          @if (loading()) {
            <div class="space-y-3" aria-live="polite">
              <div class="h-4 w-1/3 animate-pulse rounded bg-gray-100 dark:bg-gray-700"></div>
              <div class="h-9 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-700"></div>
              <span class="sr-only">Checking what publishing this agent would share…</span>
            </div>
          } @else if (blockReason(); as reason) {
            <!-- D7.2 — not a warning. Nothing below it would help. -->
            <div
              role="alert"
              class="flex gap-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-200"
            >
              <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-5 shrink-0" aria-hidden="true" />
              <p>{{ reason }}</p>
            </div>
          } @else {
            <!-- Going public. Above the form because it is the one thing here that changes
                 who can reach the agent, and it is a *choice*, not a warning: the author
                 acts on it in place rather than being sent to another screen. -->
            @if (requiresPublic()) {
              <div
                class="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-900 dark:bg-amber-900/20"
              >
                <div class="flex gap-3">
                  <input
                    id="listing-make-public"
                    type="checkbox"
                    [checked]="makePublic()"
                    (change)="onMakePublicChange($event)"
                    [attr.aria-describedby]="'listing-make-public-help'"
                    class="mt-1 size-4 shrink-0 rounded border-gray-300 text-blue-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <div>
                    <label
                      for="listing-make-public"
                      class="block text-sm/6 font-medium text-amber-900 dark:text-amber-200"
                    >
                      Make this agent public
                    </label>
                    <p id="listing-make-public-help" class="text-sm/6 text-amber-900 dark:text-amber-200">
                      {{ makePublicHelp() }}
                    </p>
                  </div>
                </div>
              </div>
            }

            <div>
              <label for="listing-category" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Category
              </label>
              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                Where it appears in Discover. An admin may move it.
              </p>
              <!--
                ⚠️ The selection lives on the options ([selected]), NOT as [value] on the
                select. The category list arrives from an await in ngOnInit, so on the first
                render the preselected category names an option that does not exist yet:
                setting select.value matches nothing, the browser silently resets to index 0,
                and when the options finally render it displays the first one. The author then
                sees a category they never chose — reading as though submitting had moved
                their listing — while the signal still holds the right value underneath.
                Binding on the option instead resolves as each one renders, which is exactly
                when the value becomes matchable.
              -->
              <select
                id="listing-category"
                (change)="onCategoryChange($event)"
                class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
              >
                <option value="" disabled [selected]="!category()">Choose a category…</option>
                @for (option of categories(); track option.id) {
                  <option [value]="option.id" [selected]="option.id === category()">
                    {{ option.label }}
                  </option>
                }
              </select>
              @if (!categories().length) {
                <p class="mt-2 text-xs/5 text-amber-700 dark:text-amber-400">
                  No categories are open for new listings yet. An admin adds these under
                  Admin → Marketplace → Categories.
                </p>
              }
            </div>

            <!--
              Tagline (D4). Prefilled from the description's first clause for the many
              Agents that predate the field — the author edits rather than invents, and
              sees what the listing will say at the one moment they are looking at it.
            -->
            <div class="mt-5">
              <label for="listing-tagline" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Tagline
              </label>
              <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                The one line under the name in Discover. We started it from your
                description — make it read like a subtitle.
              </p>
              <input
                id="listing-tagline"
                type="text"
                [value]="tagline()"
                (input)="onTaglineInput($event)"
                [attr.maxlength]="taglineMax"
                class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
              />
              <p class="mt-1 text-right text-xs/5 text-gray-400 dark:text-gray-500">
                {{ tagline().length }}/{{ taglineMax }}
              </p>
            </div>

            <div class="mt-5">
              <label for="listing-note" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Note to the admin <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
              </label>
              <textarea
                id="listing-note"
                rows="3"
                [value]="note()"
                (input)="onNoteInput($event)"
                [placeholder]="notePlaceholder()"
                class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
              ></textarea>
            </div>

            <!-- D7.1 — enumerate, don't count. -->
            @if (exposedSkills().length; as count) {
              <div class="mt-5 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-900 dark:bg-amber-900/20">
                <div class="flex gap-3">
                  <ng-icon
                    name="heroEye"
                    class="mt-0.5 size-5 shrink-0 text-amber-700 dark:text-amber-400"
                    aria-hidden="true"
                  />
                  <div class="min-w-0">
                    <p class="text-sm/6 font-medium text-amber-900 dark:text-amber-200">
                      {{ count === 1 ? '1 skill you wrote becomes' : count + ' skills you wrote become' }}
                      readable by anyone who runs this agent
                    </p>
                    <ul class="mt-1.5 space-y-0.5">
                      @for (skill of exposedSkills(); track skill.ref) {
                        <li class="text-sm/6 text-amber-900 dark:text-amber-200">· {{ skill.label }}</li>
                      }
                    </ul>
                  </div>
                </div>
              </div>
            }

            <p class="mt-5 text-xs/5 text-gray-500 dark:text-gray-400">
              @if (data.listing) {
                This keeps whoever the listing is already credited to.
              } @else {
                You'll be credited as the publisher.
              }
              An admin may reattribute the listing to a department or the university at
              approval — that changes the name shown with it in Discover and nothing about who
              can run it.
            </p>

            @if (error(); as message) {
              <p role="alert" class="mt-4 text-sm/6 text-rose-700 dark:text-rose-400">{{ message }}</p>
            }
          }
        </div>

        <div class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            {{ blockReason() ? 'Close' : 'Cancel' }}
          </button>
          @if (!blockReason()) {
            <button
              type="button"
              [disabled]="!canSubmit()"
              (click)="onSubmit()"
              class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              {{ submitting() ? 'Submitting…' : 'Submit for review' }}
            </button>
          }
        </div>
      </div>
    </div>
  `,
})
export class SubmitListingDialogComponent implements OnInit {
  private dialogRef = inject<DialogRef<SubmitListingDialogResult>>(DialogRef);
  private listings = inject(AgentListingService);
  readonly data = inject<SubmitListingDialogData>(DIALOG_DATA);

  readonly categories = signal<AgentCategory[]>([]);
  readonly exposedSkills = signal<SkillExposure[]>([]);
  readonly blockReason = signal<string | null>(null);
  /**
   * This agent is not PUBLIC yet. Not a block: the checkbox below resolves it, and
   * submitting widens visibility in the same write. The marketplace is public-only, and
   * every agent starts PRIVATE, so this is the ordinary first-submission path.
   */
  readonly requiresPublic = signal(false);
  /** The author's consent. Starts unticked — going public is a decision, not a default. */
  readonly makePublic = signal(false);
  /** The current reachability, kept to word the consent copy for what it actually changes. */
  private readonly reachability = signal<ListingReachability>('everyone');
  readonly loading = signal(true);
  readonly submitting = signal(false);
  readonly error = signal<string | null>(null);

  readonly category = signal('');
  readonly note = signal('');
  readonly tagline = signal('');
  readonly taglineMax = TAGLINE_MAX;

  readonly isResubmission = computed(() => !!this.data.listing);

  /**
   * A resubmission over something users can currently see — which changes what the dialog
   * can honestly promise. "An admin reviews this before it appears in the store" is false
   * for a listing already in the store, and reads as though submitting took it down.
   *
   * `publishedVersion` is the fact, not the state name: a listing an admin sent back for
   * changes keeps serving while the author revises it.
   */
  readonly isUpdate = computed(() => !!this.data.listing?.publishedVersion);

  readonly canSubmit = computed(
    () =>
      !!this.category() &&
      !this.submitting() &&
      !this.blockReason() &&
      // Consent is required, not implied: the backend refuses an omitted flag, so a
      // Submit that looked enabled here would fail on the round trip.
      (!this.requiresPublic() || this.makePublic()),
  );

  /**
   * Says what going public actually changes *from*, so "shared with 3 people → everyone"
   * and "only me → everyone" do not read as the same sentence.
   */
  readonly makePublicHelp = computed(() =>
    this.reachability() === 'shared_only'
      ? 'Right now only the people it is shared with can open it. The store is public, so ' +
        'publishing it makes it available to everyone at Boise State.'
      : 'Right now only you can open it. The store is public, so publishing it makes it ' +
        'available to everyone at Boise State.',
  );

  /** A resubmission is answering an admin; a first submission is introducing itself. */
  readonly notePlaceholder = computed(() =>
    this.isResubmission()
      ? 'What you changed since the last review.'
      : 'Anything the admin should know — who it is for, what it draws on.',
  );

  async ngOnInit(): Promise<void> {
    // Preselect the category the listing already had, so a resubmission is one click.
    this.category.set(this.data.listing?.category ?? '');
    // Keep an existing tagline; derive one only when the Agent has never had it, which
    // is every Agent that predates the field (#749).
    this.tagline.set(
      (this.data.tagline ?? '').trim() || deriveTagline(this.data.description),
    );
    try {
      const [categories, preflight] = await Promise.all([
        this.listings.loadCategories(),
        this.listings.preflight(this.data.agentId),
      ]);
      this.categories.set(categories);
      this.exposedSkills.set(preflight.exposedSkills ?? []);
      this.blockReason.set(preflight.blockReason ?? null);
      this.requiresPublic.set(preflight.requiresPublic ?? false);
      this.reachability.set(preflight.reachability);
      // Only preselect a category that is still open for new listings.
      if (!categories.some((c) => c.id === this.category())) {
        this.category.set('');
      }
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Could not check this agent for submission.');
    } finally {
      this.loading.set(false);
    }
  }

  onCategoryChange(event: Event): void {
    this.category.set((event.target as HTMLSelectElement).value);
  }

  onNoteInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  onTaglineInput(event: Event): void {
    this.tagline.set((event.target as HTMLInputElement).value);
  }

  onMakePublicChange(event: Event): void {
    this.makePublic.set((event.target as HTMLInputElement).checked);
  }

  async onSubmit(): Promise<void> {
    if (!this.canSubmit()) return;
    this.submitting.set(true);
    this.error.set(null);
    try {
      const response = await this.listings.submit(this.data.agentId, {
        category: this.category(),
        note: this.note().trim() || undefined,
        // Sent only when it is actually being asked for, so an already-public agent's
        // request does not carry a consent it never sought.
        makePublic: this.requiresPublic() ? this.makePublic() : undefined,
        // Omitted rather than blanked when empty — the backend reads `undefined` as
        // "leave the existing tagline alone".
        tagline: this.tagline().trim() || undefined,
      });
      this.dialogRef.close(response.listing);
    } catch (err) {
      // The backend re-runs both D7 checks on the write, so a binding added since the
      // preflight surfaces here rather than passing silently.
      this.error.set(this.detail(err) ?? 'Failed to submit this agent for review.');
      this.submitting.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }

  private detail(err: unknown): string | null {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : null;
  }
}
