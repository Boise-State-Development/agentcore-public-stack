import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronRight, heroLightBulb } from '@ng-icons/heroicons/outline';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';

/**
 * A one-line pointer, above Recents on the project page, to memory changes waiting for
 * review (shared-projects 2.5a-1). Reviewing happens in the Memory tab's review queue
 * (2.8), which shows the current and proposed file side by side; this only says how many
 * are waiting and links there.
 *
 * Editors and the owner see every pending proposal; anyone else sees their own. Renders
 * nothing when nothing is pending, or when the project has no shared memory, so most
 * visits see no change to the page.
 */
@Component({
  selector: 'app-project-proposals',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroChevronRight, heroLightBulb })],
  template: `
    @if (count() > 0) {
      <section aria-labelledby="proposals-heading" class="mb-10">
        <a
          [routerLink]="['/projects', project().projectId, 'memory']"
          [queryParams]="{ view: 'review' }"
          class="group flex items-center gap-3 rounded-2xl border border-gray-200 px-4 py-3 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:hover:bg-white/5"
        >
          <ng-icon name="heroLightBulb" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
          <span class="min-w-0 flex-1">
            <span id="proposals-heading" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
              {{ canReview() ? 'Proposed memory changes' : 'Your proposed memory changes' }}
              <span class="ml-1 rounded-full bg-gray-100 px-2 text-xs/5 font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-200">{{ count() }}</span>
            </span>
            <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
              {{ canReview()
                ? (count() === 1 ? 'One change is waiting for review.' : count() + ' changes are waiting for review.')
                : 'An editor will review them. You’ll get a notification when they decide.' }}
            </span>
          </span>
          <span class="flex shrink-0 items-center gap-1 text-sm/6 text-gray-600 group-hover:text-gray-900 dark:text-gray-400 dark:group-hover:text-white">
            {{ canReview() ? 'Review' : 'View' }}
            <ng-icon name="heroChevronRight" class="size-4" aria-hidden="true" />
          </span>
        </a>
      </section>
    }
  `,
})
export class ProjectProposalsComponent {
  private api = inject(ProjectApiService);

  readonly project = input.required<Project>();

  protected readonly count = signal(0);

  /** Owner or editor of an active project. An archived project takes no decisions. */
  protected readonly canReview = computed(() => {
    const p = this.project();
    return p.status === 'active' && (p.role === 'owner' || p.role === 'editor');
  });

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.count.set(0);
        void this.load(id);
      });
    });
  }

  private async load(projectId: string): Promise<void> {
    try {
      const response = await firstValueFrom(this.api.proposals(projectId, 'pending'));
      if (projectId === this.projectId()) this.count.set(response.proposals.length);
    } catch {
      // Memory may be off for this deployment, or the project made before it: no section, no noise.
      // The Memory tab reports a failure where reviewing happens.
    }
  }
}
