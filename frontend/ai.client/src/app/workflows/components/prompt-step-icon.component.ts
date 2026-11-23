import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSparkles } from '@ng-icons/heroicons/outline';

/**
 * A prompt step's mark (docs/specs/workflow-designer.md §21.3), where an Agent step shows its
 * Agent's icon: a prompt step has no Agent, only a model. Decorative: the step's label always
 * says "Prompt" in text beside it.
 */
@Component({
  selector: 'app-prompt-step-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroSparkles })],
  host: { class: 'inline-flex shrink-0', 'aria-hidden': 'true' },
  template: `
    <span
      class="grid place-items-center rounded-lg bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50"
      [style.width.px]="size()"
      [style.height.px]="size()"
    >
      <ng-icon name="heroSparkles" [style.width.px]="glyph()" [style.height.px]="glyph()" />
    </span>
  `,
})
export class PromptStepIconComponent {
  readonly size = input(28);
  protected readonly glyph = computed(() => Math.round(this.size() * 0.57));
}
