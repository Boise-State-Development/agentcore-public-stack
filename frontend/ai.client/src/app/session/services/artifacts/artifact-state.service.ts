import { Injectable, computed, signal } from '@angular/core';
import type { ArtifactEvent } from '../../../shared/utils/stream-parser';
import type { Artifact, OpenArtifactRef } from './artifact.model';

/**
 * Per-session artifact registry + open-panel state. Structural sibling of
 * `CompactionSummaryService`: a live SSE path (`recordLive`), a
 * session-load hydration path (`seedFromHydration`), and a `reset()`
 * called on session change.
 *
 * Artifacts are keyed by `artifactId` holding only the highest version
 * seen — `update_artifact` emits a new event for the same id and the
 * card should track the latest. Hydration never clobbers a higher
 * version already recorded from a live event (a stale list response can
 * arrive after the user has already driven a newer update this session).
 */
@Injectable({ providedIn: 'root' })
export class ArtifactStateService {
  private readonly byId = signal<Map<string, Artifact>>(new Map());
  private readonly openRef = signal<OpenArtifactRef | null>(null);

  /** Artifacts for the current session, newest update first. */
  readonly artifacts = computed<Artifact[]>(() =>
    Array.from(this.byId().values()).sort((a, b) =>
      b.updatedAt.localeCompare(a.updatedAt),
    ),
  );

  readonly hasArtifacts = computed(() => this.byId().size > 0);

  /** The artifact the side panel is showing, or null when closed. */
  readonly openArtifact = this.openRef.asReadonly();

  /** Latest known version of an artifact, if any (used by the card). */
  get(artifactId: string): Artifact | undefined {
    return this.byId().get(artifactId);
  }

  /**
   * Record a live `artifact` SSE event. Keeps the highest version.
   *
   * `producedByMessageId` is the concrete id of the assistant message
   * that just streamed (resolved by the caller the same way oauth /
   * tool-approval prompts are). Live placement keys off this rather than
   * the numeric index, which only lines up after a reload.
   */
  recordLive(event: ArtifactEvent, producedByMessageId?: string | null): void {
    this.upsert({
      artifactId: event.artifactId,
      version: event.version,
      title: event.title,
      contentType: event.contentType,
      updatedAt: event.updatedAt,
      producedByMessageIndex: event.producedByMessageIndex ?? null,
      producedByMessageId: producedByMessageId ?? null,
    });
  }

  /**
   * Replay the persisted session artifact list on load. Idempotent and
   * non-clobbering: an entry already at an equal-or-higher version (from
   * a live event that raced ahead) is left untouched.
   */
  seedFromHydration(list: readonly Artifact[]): void {
    for (const a of list) this.upsert(a);
  }

  openArtifactPanel(ref: OpenArtifactRef): void {
    this.openRef.set(ref);
  }

  closeArtifactPanel(): void {
    this.openRef.set(null);
  }

  /** Clear all state — called on session change. */
  reset(): void {
    this.byId.set(new Map());
    this.openRef.set(null);
  }

  private upsert(a: Artifact): void {
    const existing = this.byId().get(a.artifactId);
    if (existing && existing.version >= a.version) return;
    this.byId.update((m) => {
      const next = new Map(m);
      next.set(a.artifactId, a);
      return next;
    });
  }
}
