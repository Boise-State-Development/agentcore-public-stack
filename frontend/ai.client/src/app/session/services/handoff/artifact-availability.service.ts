import { Injectable, inject, signal } from '@angular/core';

import { ArtifactHttpService } from '../artifacts/artifact-http.service';

/** What a delivery's artifact reference points at now (handoffs spec §4.8, "Artifacts on a delivery"). */
export type ArtifactAvailability = 'available' | 'newer' | 'gone' | 'unknown';

/** A listing this old is read again before a chip trusts it, so a just-made artifact isn't "gone". */
const FRESH_MS = 30_000;

/**
 * Whether the artifacts a card or sent bubble references still exist, and at which version.
 *
 * One read of the user's artifact library serves every chip on the page, because artifacts are the
 * user's and a reference can come from any of their conversations. Until it lands, or if it fails,
 * a chip reads `unknown` and renders as a plain link: the viewer says the rest.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactAvailabilityService {
  private readonly http = inject(ArtifactHttpService);
  private readonly versions = signal<ReadonlyMap<string, number> | null>(null);
  private loadedAt = 0;
  private inFlight: Promise<void> | null = null;

  /** Read the library if the copy held is missing or stale. Call when chips render. */
  refresh(): void {
    if (this.inFlight || Date.now() - this.loadedAt < FRESH_MS) return;
    this.inFlight = this.http
      .listLibrary()
      .then((items) => {
        this.versions.set(new Map(items.map((a) => [a.artifactId, a.version])));
        this.loadedAt = Date.now();
      })
      .catch(() => {
        // Leave chips as plain links; the viewer handles a missing artifact itself.
      })
      .finally(() => {
        this.inFlight = null;
      });
  }

  /** A reference's state against the library: the pinned version, a newer one, or none at all. */
  availability(artifactId: string, pinnedVersion: number): ArtifactAvailability {
    const versions = this.versions();
    if (!versions) return 'unknown';
    const current = versions.get(artifactId);
    if (current === undefined) return 'gone';
    return current > pinnedVersion ? 'newer' : 'available';
  }
}
