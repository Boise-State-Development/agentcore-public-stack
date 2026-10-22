import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  input,
  output,
  viewChild,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight, heroSparkles } from '@ng-icons/heroicons/outline';
import { SkillCommand } from '../../../services/skill/skill-command.service';

/**
 * The `/` menu over the composer.
 *
 * The sibling of the `@` menu, and deliberately built the same way: rows commit on
 * `mousedown` rather than `click`, because the composer's textarea must keep focus through
 * the whole interaction — a `click` handler fires after `blur`, by which time the caret
 * position the parent edits around is gone. Keyboard handling also lives in the parent for
 * that reason; this component renders the highlight it is told about (`activeIndex`) and
 * exposes the ids `aria-activedescendant` on the textarea points at.
 *
 * What it lists is the skills the user has turned on, which is exactly what the turn already
 * discloses to the model. "Browse skills →" is the last row: turning a skill on belongs on
 * the Customize page, not in an autocomplete.
 *
 * Surfaces are neutral and the brand blue lives in the glyph and the slug. Do not reach for
 * `bg-primary-50/100/200` as a tint here — the primary scale is derived from #0033a0 by
 * lightness offset alone and keeps full chroma, so those steps are saturated mid-blues, not
 * the pale washes their names suggest.
 */
@Component({
  selector: 'app-skill-command-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowRight, heroSparkles })],
  template: `
    <div
      class="absolute bottom-full left-0 z-20 mb-2 w-full max-w-md overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800"
    >
      <ul
        #listbox
        [id]="listboxId"
        role="listbox"
        aria-label="Skills you can run"
        class="max-h-72 overflow-y-auto py-1"
      >
        <li
          class="px-4 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
          role="presentation"
        >
          Skills
        </li>

        @if (items().length === 0) {
          <li class="px-4 py-3 text-sm/6 text-gray-500 dark:text-gray-400" role="presentation">
            No skills match “{{ query() }}”.
          </li>
        }

        @for (command of items(); track command.skillId; let index = $index) {
          <li
            [id]="optionId(index)"
            role="option"
            [attr.aria-selected]="index === activeIndex()"
            (mousedown)="onPick($event, command)"
            class="flex cursor-pointer items-start gap-3 px-4 py-2"
            [class]="
              index === activeIndex()
                ? 'bg-gray-100 dark:bg-gray-700'
                : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
            "
          >
            <span
              class="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50"
              aria-hidden="true"
            >
              <ng-icon name="heroSparkles" class="size-4" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                <span class="font-mono">/{{ command.slug }}</span>
                <span class="ml-2 font-normal text-gray-500 dark:text-gray-400">
                  {{ command.name }}
                </span>
              </p>
              @if (command.description) {
                <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                  {{ command.description }}
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
        Browse skills
        <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
      </button>
    </div>
  `,
})
export class SkillCommandMenuComponent {
  readonly items = input.required<SkillCommand[]>();
  readonly activeIndex = input<number>(0);
  readonly query = input<string>('');

  readonly picked = output<SkillCommand>();
  readonly browseAll = output<void>();

  readonly listboxId = 'skill-command-listbox';

  private readonly listbox = viewChild<ElementRef<HTMLUListElement>>('listbox');

  /**
   * Keep the highlighted row visible. The list scrolls at eight rows (`max-h-72`) and the
   * parent owns the keyboard, so without this, arrowing down would move a highlight the
   * user cannot see.
   */
  private readonly scrollActiveIntoView = effect(() => {
    const index = this.activeIndex();
    const list = this.listbox()?.nativeElement;
    if (!list) {
      return;
    }
    // Positional lookup: the "Skills" heading is `role="presentation"`, so the option
    // elements line up one-to-one with the flat index the parent counts in.
    const option = list.querySelectorAll<HTMLElement>('[role="option"]')[index];
    option?.scrollIntoView?.({ block: 'nearest' });
  });

  optionId(index: number): string {
    return `skill-command-option-${index}`;
  }

  onPick(event: MouseEvent, command: SkillCommand): void {
    // Keep the textarea focused: the parent edits around the caret this menu was opened
    // from, and a blur would lose it.
    event.preventDefault();
    this.picked.emit(command);
  }

  onBrowseAll(event: MouseEvent): void {
    event.preventDefault();
    this.browseAll.emit();
  }
}
