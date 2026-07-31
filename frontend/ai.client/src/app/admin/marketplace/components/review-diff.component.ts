import { ChangeDetectionStrategy, Component, inject, input, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroChevronRight } from '@ng-icons/heroicons/outline';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AgentVersionDiff, VersionFieldChange } from '../models/marketplace.model';

/**
 * "What changed since I approved this?" — the reviewer's actual question (§6.1).
 *
 * Before this, a submission arrived in the queue with no reference to what it replaces, so
 * a typo fix and a full instruction rewrite looked identical and both got the same careful
 * read. The point is asymmetry: make the cheap approval cheap without making the expensive
 * one easy to miss.
 *
 * **Collapsed by default, loaded on expand.** The queue is a list of decisions; fetching a
 * diff for every row up front would pull every pending Agent's full instructions down to
 * render a badge nobody has opened. The one thing shown *before* expanding is whether
 * behavior changed, because that is what decides whether the row needs opening at all —
 * and it rides the queue payload rather than this request.
 */
@Component({
  selector: 'app-review-diff',
  imports: [NgIcon],
  providers: [provideIcons({ heroChevronDown, heroChevronRight })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="mt-2">
      <button
        type="button"
        (click)="toggle()"
        [attr.aria-expanded]="expanded()"
        [attr.aria-controls]="panelId()"
        class="inline-flex items-center gap-1 rounded-2xl text-sm/6 font-medium text-blue-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400"
      >
        <ng-icon
          [name]="expanded() ? 'heroChevronDown' : 'heroChevronRight'"
          class="size-4"
          aria-hidden="true"
        />
        {{ expanded() ? 'Hide changes' : 'What changed' }}
      </button>

      @if (expanded()) {
        <div [id]="panelId()" class="mt-2 text-sm/6">
          @if (loading()) {
            <p class="text-gray-500 dark:text-gray-400">Loading changes…</p>
          } @else if (error(); as message) {
            <p class="text-rose-700 dark:text-rose-400">{{ message }}</p>
          } @else if (diff(); as d) {
            @if (d.firstSubmission) {
              <!-- Not "nothing changed" — there is simply nothing to compare against, and
                   the reviewer is reading the whole submission rather than a delta. -->
              <p class="text-gray-600 dark:text-gray-400">
                First submission — nothing is published yet, so there is nothing to compare
                against.
              </p>
            } @else if (d.changes.length === 0) {
              <p class="text-gray-600 dark:text-gray-400">
                Identical to the published version {{ d.publishedVersion }}.
              </p>
            } @else {
              <p class="text-gray-600 dark:text-gray-400">
                Version {{ d.pendingVersion }} against published
                {{ d.publishedVersion }}
              </p>

              <ul class="mt-2 flex flex-wrap gap-1.5">
                @for (change of d.changes; track change.field) {
                  <li
                    class="rounded-2xl px-2 py-0.5 text-xs font-medium"
                    [class]="change.behavior ? behaviorClasses : presentationClasses"
                  >
                    {{ label(change) }}
                  </li>
                }
              </ul>

              @if (d.instructionsDiff.length) {
                <!-- Monospace and horizontally scrollable: instructions are prose the author
                     wrote with intentional line breaks, and wrapping them would invent
                     changes that are not there. -->
                <pre
                  class="mt-3 overflow-x-auto rounded-2xl bg-gray-50 p-3 text-xs/5 dark:bg-gray-900"
                ><code>@for (line of d.instructionsDiff; track $index) {<span [class]="lineClasses(line)">{{ line }}
</span>}</code></pre>
              }
            }
          }
        </div>
      }
    </div>
  `,
})
export class ReviewDiffComponent {
  private readonly service = inject(AdminMarketplaceService);

  readonly agentId = input.required<string>();

  protected readonly expanded = signal(false);
  protected readonly loading = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly diff = signal<AgentVersionDiff | null>(null);

  /** Behavior reads as attention; presentation reads as information. */
  protected readonly behaviorClasses =
    'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300';
  protected readonly presentationClasses =
    'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300';

  private readonly fieldLabels: Record<string, string> = {
    instructions: 'Instructions',
    bindings: 'Tools & skills',
    modelConfig: 'Model',
    name: 'Name',
    description: 'Description',
    tagline: 'Tagline',
    starters: 'Starters',
    emoji: 'Emoji',
    iconKey: 'Icon',
    category: 'Category',
    publisherId: 'Publisher',
  };

  protected panelId(): string {
    return `review-diff-${this.agentId()}`;
  }

  protected label(change: VersionFieldChange): string {
    return this.fieldLabels[change.field] ?? change.field;
  }

  /** Colour the +/- lines; the `@@` hunk headers and context stay neutral. */
  protected lineClasses(line: string): string {
    if (line.startsWith('+++') || line.startsWith('---')) {
      return 'text-gray-500 dark:text-gray-400';
    }
    if (line.startsWith('+')) {
      return 'text-emerald-700 dark:text-emerald-400';
    }
    if (line.startsWith('-')) {
      return 'text-rose-700 dark:text-rose-400';
    }
    return 'text-gray-600 dark:text-gray-300';
  }

  protected async toggle(): Promise<void> {
    const next = !this.expanded();
    this.expanded.set(next);
    // Fetched once and kept: a reviewer comparing two rows should not re-request, and the
    // pending version cannot change under them while they read it — it is immutable.
    if (next && !this.diff() && !this.loading()) {
      await this.load();
    }
  }

  /**
   * Load the diff, preferring the server's own explanation over a generic one.
   *
   * ⚠️ **The reason a diff is missing is usually the useful part.** This used to discard the
   * response entirely and always say "could not load", which reads as a transient failure —
   * so a reviewer looking at a submission that simply *predates* version snapshots was told
   * to try again, and trying again could never work. The backend distinguishes those cases
   * deliberately (it names the pre-snapshot one and tells the author to resubmit); throwing
   * that away at the boundary put the reviewer back where the collapsed message left them.
   *
   * The generic line stays as the fallback for the case it was written for: a real transport
   * failure, where there is no `detail` and retrying is exactly the right advice.
   */
  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.diff.set(await this.service.loadDiff(this.agentId()));
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.error.set(
        typeof detail === 'string' && detail.trim()
          ? detail
          : 'Could not load what changed. Try again, or review the agent directly.',
      );
    } finally {
      this.loading.set(false);
    }
  }
}
