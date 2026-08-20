import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight } from '@ng-icons/heroicons/outline';
import { AgentIconComponent } from '../../../agents/components/agent-icon.component';
import { MentionableAgent } from '../../../agents/services/agent-mention.service';

/**
 * The `@` menu over the composer (Marketplace D11).
 *
 * Renders what the user can hand a turn to — their own Agents and everything pinned —
 * grouped, with the publisher (or tagline) as secondary text, and "Browse all agents →" as
 * the last row. The store is deliberately *not* searchable from here; that row is the door
 * to it.
 *
 * **Rows commit on `mousedown`, not `click`.** The composer's textarea must keep focus
 * through the whole interaction — a `click` handler fires after `blur`, by which time the
 * caret position this menu edits around is gone.
 *
 * Keyboard handling lives in the parent, not here, for the same reason: the textarea never
 * gives up focus, so arrow keys and Enter arrive there. This component only renders the
 * highlight it is told about (`activeIndex`) and exposes the ids that
 * `aria-activedescendant` on the textarea points at.
 */
@Component({
  selector: 'app-agent-mention-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent, NgIcon],
  providers: [provideIcons({ heroArrowRight })],
  template: `
    <div
      class="absolute bottom-full left-0 z-20 mb-2 w-full max-w-md overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800"
    >
      <ul
        [id]="listboxId()"
        role="listbox"
        aria-label="Agents you can mention"
        class="max-h-72 overflow-y-auto py-1"
      >
        @if (items().length === 0) {
          <li class="px-4 py-3 text-sm/6 text-gray-500 dark:text-gray-400" role="presentation">
            No agents match “{{ query() }}”.
          </li>
        }

        @for (entry of grouped(); track entry.agent.agentId) {
          @if (entry.heading) {
            <li
              class="px-4 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
              role="presentation"
            >
              {{ entry.heading }}
            </li>
          }
          <li
            [id]="optionId(entry.index)"
            role="option"
            [attr.aria-selected]="entry.index === activeIndex()"
            (mousedown)="onPick($event, entry.agent)"
            class="flex cursor-pointer items-center gap-3 px-4 py-2"
            [class]="
              entry.index === activeIndex()
                ? 'bg-gray-100 dark:bg-gray-700'
                : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
            "
          >
            <app-agent-icon
              [agentId]="entry.agent.agentId"
              [iconUrl]="entry.agent.iconUrl"
              [emoji]="entry.agent.emoji"
              [size]="28"
            />
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                {{ entry.agent.name }}
              </p>
              @if (entry.agent.subtitle) {
                <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                  {{ entry.agent.subtitle }}
                </p>
              }
            </div>
          </li>
        }
      </ul>

      <button
        type="button"
        (mousedown)="onBrowseAll($event)"
        class="flex w-full items-center justify-between gap-2 border-t border-gray-200 px-4 py-2.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-700/50"
      >
        Browse all agents
        <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
      </button>
    </div>
  `,
})
export class AgentMentionMenuComponent {
  readonly items = input.required<MentionableAgent[]>();
  readonly activeIndex = input<number>(0);
  readonly query = input<string>('');

  readonly picked = output<MentionableAgent>();
  readonly browseAll = output<void>();

  readonly listboxId = () => 'agent-mention-listbox';

  optionId(index: number): string {
    return `agent-mention-option-${index}`;
  }

  /**
   * Rows with their flat index and, on the first of each group, its heading.
   *
   * The index has to be the *flat* one because that is what the parent's arrow keys and
   * `aria-activedescendant` count in; the headings are presentational and must not be
   * separately navigable.
   */
  readonly grouped = computed(() => {
    let lastGroup: string | null = null;
    return this.items().map((agent, index) => {
      const heading =
        agent.group === lastGroup
          ? null
          : agent.group === 'own'
            ? 'Your agents'
            : 'Pinned';
      lastGroup = agent.group;
      return { agent, index, heading };
    });
  });

  onPick(event: MouseEvent, agent: MentionableAgent): void {
    // Keep the textarea focused: the parent edits around the caret this menu was
    // opened from, and a blur would lose it.
    event.preventDefault();
    this.picked.emit(agent);
  }

  onBrowseAll(event: MouseEvent): void {
    event.preventDefault();
    this.browseAll.emit();
  }
}
