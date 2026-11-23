import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroClipboardDocumentCheck } from '@ng-icons/heroicons/outline';

/**
 * A checklist step's mark (docs/specs/workflow-designer.md §22.3), where an Agent step shows its
 * Agent's icon: a checklist step has no Agent, only a model and its field sets. Decorative: the
 * step's label always says "Checklist" in text beside it. The prompt step's tile, with its own glyph.
 */
@Component({
  selector: 'app-checklist-step-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroClipboardDocumentCheck })],
  host: { class: 'inline-flex shrink-0', 'aria-hidden': 'true' },
  template: `
    <span
      class="grid place-items-center rounded-lg bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50"
      [style.width.px]="size()"
      [style.height.px]="size()"
    >
      <ng-icon name="heroClipboardDocumentCheck" [style.width.px]="glyph()" [style.height.px]="glyph()" />
    </span>
  `,
})
export class ChecklistStepIconComponent {
  readonly size = input(28);
  protected readonly glyph = computed(() => Math.round(this.size() * 0.57));
}
