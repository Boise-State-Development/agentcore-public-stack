import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { MemoryLintFinding } from '../models/project.model';

/**
 * What the content check found in one item or description (shared-projects 2.7), one line
 * each, under the text it is about.
 *
 * The check is a flag for the people who read memory, never a change to it: the API
 * computes it on read and nothing of it reaches a task. A credential is named by kind
 * ("an AWS access key"), never quoted.
 */
@Component({
  selector: 'app-memory-lint-flags',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    @for (finding of findings(); track $index) {
      <p class="mt-1 flex items-start gap-1.5 text-xs/5 text-state-warning-800 dark:text-state-warning-300">
        <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <span><span class="font-medium">Content check:</span> {{ finding.summary }}</span>
      </p>
    }
  `,
})
export class MemoryLintFlagsComponent {
  readonly findings = input.required<readonly MemoryLintFinding[]>();
}
