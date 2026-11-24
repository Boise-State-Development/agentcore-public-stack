import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { SkillScriptStatus, reviewDate, scriptsVersion } from '../script-review.model';

/** The badge's line: who stands behind one skill's scripts. */
export function scriptsTrustLine(status: SkillScriptStatus): string {
  const date = reviewDate(status.reviewedAt);
  const when = date ? `, ${date}` : '';
  if (status.state === 'admin') return 'From the admin catalog';
  if (status.state === 'authored') return status.author ? `Shared by ${status.author}${when}` : `Shared by its author${when}`;
  return status.reviewer ? `Reviewed by ${status.reviewer}${when}` : `Reviewed${when}`;
}

/**
 * "Runs scripts in a sandbox with no internet access" on an Agent's or project's details
 * (docs/specs/skill-code-sandbox.md §8.6): what members see instead of a prompt. It
 * appears only when a bound skill has scripts that run for the viewer, which is what
 * `GET …/skill-scripts` returns to anyone who isn't an editor.
 */
@Component({
  selector: 'app-skill-scripts-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (runnable().length === 1) {
      @let only = runnable()[0];
      <p class="text-sm/6 text-gray-700 dark:text-gray-300">
        <span class="font-medium text-gray-900 dark:text-white">Runs scripts in a sandbox with no internet access</span>
        · {{ trustLine(only) }}
      </p>
    } @else if (runnable().length > 1) {
      <details class="group text-sm/6 text-gray-700 dark:text-gray-300">
        <summary class="cursor-pointer font-medium text-gray-900 marker:text-gray-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-white">
          Runs scripts from {{ runnable().length }} skills in a sandbox with no internet access
        </summary>
        <ul class="mt-1 space-y-0.5 pl-4">
          @for (s of runnable(); track s.skillId) {
            <li>{{ s.displayName }} · version {{ version(s) }} · {{ trustLine(s) }}</li>
          }
        </ul>
      </details>
    }
  `,
})
export class SkillScriptsBadgeComponent {
  /** The member view of `GET /agents/{id}/skill-scripts` or `/projects/{id}/skill-scripts`. */
  readonly skills = input<readonly SkillScriptStatus[]>([]);

  protected readonly runnable = computed(() =>
    this.skills().filter((s) => s.state === 'allowed' || s.state === 'authored' || s.state === 'admin'),
  );

  protected trustLine(status: SkillScriptStatus): string {
    return scriptsTrustLine(status);
  }

  protected version(status: SkillScriptStatus): string {
    return scriptsVersion(status.scriptsHash);
  }
}
