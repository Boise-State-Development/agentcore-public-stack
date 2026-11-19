import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationCircle, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { SkillMdDiagnostic } from './skill-md-parse';

/**
 * Lists a `SKILL.md`'s diagnostics, errors first. Each row says "Error" or
 * "Warning" in words (and an icon), not by colour alone. Every message is
 * third-party-derived text, so it is rendered by interpolation only.
 */
@Component({
  selector: 'app-skill-md-diagnostics',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  viewProviders: [provideIcons({ heroExclamationCircle, heroExclamationTriangle })],
  template: `
    @if (sorted().length > 0) {
      <div class="mt-3" data-testid="skill-md-diagnostics">
        <p [id]="headingId()" class="text-sm/6 font-medium text-gray-900 dark:text-white">
          {{ summary() }}
        </p>
        <ul class="mt-1 space-y-1" [attr.aria-labelledby]="headingId()">
          @for (d of sorted(); track $index) {
            <li
              class="flex items-start gap-2 rounded-lg px-2 py-1 text-sm/6"
              [class]="
                d.level === 'error'
                  ? 'bg-state-danger-50 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200'
                  : 'bg-state-warning-50 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-200'
              "
              [attr.data-level]="d.level"
            >
              <ng-icon
                [name]="d.level === 'error' ? 'heroExclamationCircle' : 'heroExclamationTriangle'"
                class="mt-1 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>
                <span class="font-semibold">{{ d.level === 'error' ? 'Error' : 'Warning' }}:</span>
                {{ d.message }}
              </span>
            </li>
          }
        </ul>
      </div>
    }
  `,
})
export class SkillMdDiagnosticsComponent {
  readonly diagnostics = input<SkillMdDiagnostic[]>([]);
  /** Distinguishes two lists on one page. */
  readonly headingId = input('skill-md-diagnostics-heading');
  /** What the notes are about, for the heading. */
  readonly subject = input('this SKILL.md');

  protected readonly sorted = computed(() => [
    ...this.diagnostics().filter((d) => d.level === 'error'),
    ...this.diagnostics().filter((d) => d.level !== 'error'),
  ]);

  protected readonly summary = computed(() => {
    const errors = this.diagnostics().filter((d) => d.level === 'error').length;
    const warnings = this.diagnostics().length - errors;
    const parts = [];
    if (errors) parts.push(`${errors} error${errors === 1 ? '' : 's'}`);
    if (warnings) parts.push(`${warnings} warning${warnings === 1 ? '' : 's'}`);
    return `${parts.join(' and ')} in ${this.subject()}`;
  });
}
