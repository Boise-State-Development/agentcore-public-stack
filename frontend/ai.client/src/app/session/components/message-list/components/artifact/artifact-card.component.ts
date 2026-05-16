import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroArrowTopRightOnSquare,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';

/**
 * Compact, clickable card for one artifact. The artifact's content is
 * never inlined here — opening the card asks the panel to mint a
 * short-lived render token and load it in a sandboxed iframe.
 *
 * Rendered as a session-level strip (next to the compaction summary),
 * not anchored to a specific message: the backend doesn't track which
 * message produced which artifact, and the panel is the real surface.
 */
@Component({
  selector: 'app-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroCodeBracket, heroArrowTopRightOnSquare }),
  ],
  template: `
    <button
      type="button"
      class="group flex w-full max-w-md items-center gap-3 rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-left transition-colors hover:border-blue-400 hover:bg-blue-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-blue-500 dark:hover:bg-gray-700/60 dark:focus-visible:ring-offset-gray-900"
      [attr.aria-label]="ariaLabel()"
      (click)="open()"
    >
      <span
        class="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300"
      >
        <ng-icon name="heroCodeBracket" class="text-lg" aria-hidden="true" />
      </span>
      <span class="min-w-0 flex-1">
        <span
          class="block truncate text-sm font-medium text-gray-900 dark:text-gray-100"
        >
          {{ artifact().title || 'Untitled artifact' }}
        </span>
        <span class="block text-xs text-gray-500 dark:text-gray-400">
          Artifact · v{{ artifact().version }}
        </span>
      </span>
      <ng-icon
        name="heroArrowTopRightOnSquare"
        class="shrink-0 text-base text-gray-400 transition-colors group-hover:text-blue-600 dark:group-hover:text-blue-300"
        aria-hidden="true"
      />
    </button>
  `,
  styles: `
    :host {
      display: block;
    }
  `,
})
export class ArtifactCardComponent {
  artifact = input.required<Artifact>();

  private artifactState = inject(ArtifactStateService);

  protected readonly ariaLabel = computed(
    () =>
      `Open artifact ${this.artifact().title || 'Untitled'}, version ${this.artifact().version}`,
  );

  protected open(): void {
    const a = this.artifact();
    this.artifactState.openArtifactPanel({
      artifactId: a.artifactId,
      version: a.version,
      title: a.title,
    });
  }
}
