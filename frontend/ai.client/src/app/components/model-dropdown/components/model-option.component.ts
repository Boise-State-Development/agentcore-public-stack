import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck } from '@ng-icons/heroicons/outline';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { ModelIconComponent } from '../../model-icon/model-icon.component';

/**
 * One model row in the chat model picker.
 *
 * Exists as a component rather than an `<ng-template>` + `ngTemplateOutlet`
 * because the same row renders in two menus — the picker's top level and its
 * "More models" submenu — and CDK's `CdkMenu` finds its items with a CONTENT
 * query. A template declared outside the menu and rendered into it with
 * `ngTemplateOutlet` belongs to the declaring view, so the query never matches
 * it: the rows would render but register as no menu items at all, taking
 * keyboard navigation and typeahead with them.
 *
 * `cdkMenuItem` goes on this component's host in the parent template, which is
 * why the row renders spans and no interactive element of its own — a `<button>`
 * inside a `role="menuitem"` host nests two controls and breaks the a11y tree.
 *
 * Layout: a left-aligned vendor avatar, then name and provider sharing the first
 * line separated by a bullet, with the description (when there is one) on a
 * second. The bullet is `aria-hidden` so the row reads as "Claude Sonnet 5
 * Anthropic" rather than "Claude Sonnet 5 bullet Anthropic"; the avatar is
 * decorative for the same reason — the row already names both.
 *
 * The avatar and the text share one wrapper rather than sitting as a third child
 * of the host: the host is `justify-between` (to push the check mark right), so a
 * bare third child would leave the avatar drifting away from the name it labels.
 *
 * Spacing around the bullet is margin (`ml-1.5` / `mr-1`), NOT template
 * whitespace: Angular strips whitespace-only text nodes by default
 * (`preserveWhitespaces: false`), so a space written between the two spans
 * silently disappears and the row renders "Claude Sonnet 5• Anthropic".
 */
@Component({
  selector: 'app-model-option',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ModelIconComponent],
  providers: [provideIcons({ heroCheck })],
  host: {
    class:
      'flex w-full cursor-pointer items-center justify-between gap-2 rounded-xs px-3 py-2 text-sm/5 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700',
  },
  template: `
    <span class="flex min-w-0 items-center gap-2.5">
      <app-model-icon [model]="model()" [size]="24" />
      <span class="min-w-0 text-left">
        <span class="block truncate">
          <span class="font-medium">{{ model().modelName }}</span>
          <span class="ml-1.5 text-xs/4 text-gray-500 dark:text-gray-400"
            ><span aria-hidden="true" class="mr-1">&bull;</span>{{ model().providerName }}</span
          >
        </span>
        <!-- Second line only when there's something to say. A model with no
             description collapses to a single line rather than leaving a blank
             one, which is most of what makes the menu shorter. -->
        @if (model().shortDescription) {
          <span class="mt-0.5 block truncate text-xs/4 text-gray-500 dark:text-gray-400">{{
            model().shortDescription
          }}</span>
        }
      </span>
    </span>

    @if (selected()) {
      <ng-icon
        name="heroCheck"
        class="size-4 shrink-0 text-primary-500 dark:text-slate-400"
        aria-hidden="true"
      />
    } @else if (showNewChatHint()) {
      <span
        class="shrink-0 rounded-sm bg-gray-100 px-1.5 py-0.5 text-[10px]/3 font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400"
        >New chat</span
      >
    }
  `,
})
export class ModelOptionComponent {
  readonly model = input.required<ManagedModel>();
  readonly selected = input<boolean>(false);
  /** Show the "New chat" hint — picking a different model starts a new session. */
  readonly showNewChatHint = input<boolean>(false);
}
