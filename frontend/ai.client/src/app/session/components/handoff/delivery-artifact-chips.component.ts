import { ChangeDetectionStrategy, Component, OnInit, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroDocumentText } from '@ng-icons/heroicons/outline';

import { ArtifactAvailabilityService } from '../../services/handoff/artifact-availability.service';
import { DeliveryArtifact } from '../../services/handoff/handoff.models';

/**
 * The artifacts a delivery references, as chips that open the artifact viewer (handoffs spec §4.8).
 *
 * The model was shown the version that was sent; the chip opens the latest. So a chip says "Newer
 * version available" once the sender revised it, and a deleted artifact reads "No longer available"
 * as plain text rather than a link that breaks.
 */
@Component({
  selector: 'app-delivery-artifact-chips',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroDocumentText })],
  template: `
    <ul class="flex flex-wrap gap-1.5" [attr.aria-label]="label()">
      @for (chip of chips(); track chip.artifact.artifactId) {
        <li>
          @if (chip.state === 'gone') {
            <span class="inline-flex max-w-64 items-center gap-1 rounded-full border border-dashed border-gray-300 px-2 py-0.5 text-xs text-gray-500 dark:border-gray-600 dark:text-gray-400">
              <ng-icon name="heroDocumentText" class="size-3.5 shrink-0" aria-hidden="true" />
              <span class="truncate line-through">{{ chip.artifact.title || 'Artifact' }}</span>
              <span class="shrink-0">· No longer available</span>
            </span>
          } @else {
            <a
              [routerLink]="['/artifacts', chip.artifact.artifactId]"
              class="inline-flex max-w-64 items-center gap-1 rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-800 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-white/10"
            >
              <ng-icon name="heroDocumentText" class="size-3.5 shrink-0" aria-hidden="true" />
              <span class="truncate">{{ chip.artifact.title || 'Artifact' }}</span>
              @if (chip.state === 'newer') {
                <span class="shrink-0 text-gray-500 dark:text-gray-400">· Newer version available</span>
              }
            </a>
          }
        </li>
      }
    </ul>
  `,
})
export class DeliveryArtifactChipsComponent implements OnInit {
  private readonly availability = inject(ArtifactAvailabilityService);

  readonly artifacts = input.required<readonly DeliveryArtifact[]>();
  /** Names the list for assistive tech, e.g. "Artifacts from Rubric for BIO 101". */
  readonly label = input('Artifacts');

  protected readonly chips = computed(() =>
    this.artifacts().map((artifact) => ({
      artifact,
      state: this.availability.availability(artifact.artifactId, artifact.version),
    })),
  );

  ngOnInit(): void {
    this.availability.refresh();
  }
}
