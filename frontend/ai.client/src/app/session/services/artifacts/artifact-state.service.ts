import { Injectable, computed, inject, signal } from '@angular/core';
import { DockedPaneService } from '../docked-pane/docked-pane.service';
import type { ArtifactEvent } from '../../../shared/utils/stream-parser';
import type { Artifact, OpenArtifactRef } from './artifact.model';

/**
 * Per-session artifact registry + open-panel state. Structural sibling of
 * `CompactionSummaryService`: a live SSE path (`recordLive`), a
 * session-load hydration path (`seedFromHydration`), and a `reset()`
 * called on session change.
 *
 * Keyed by `artifactId#version` — every version is its own entry so the
 * conversation can show one card per version. `update_artifact` emits a
 * new event for a new version (a new key), not a replacement. When the
 * same (id, version) arrives from both a live event and reload
 * hydration, the live entry wins: it carries `producedByMessageId`, the
 * precise per-turn anchor the index-only hydration row lacks.
 *
 * The docked pane itself (its width, the side-nav choreography, and
 * which feature currently holds it) belongs to `DockedPaneService` —
 * the .docx preview shares the same rail. This service owns only
 * *which artifact* is showing, and gates that behind the rail's current
 * owner so being evicted needs no callback.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactStateService {
  private readonly dockedPane = inject(DockedPaneService);

  private readonly byKey = signal<Map<string, Artifact>>(new Map());
  private readonly openRef = signal<OpenArtifactRef | null>(null);

  /** Every artifact version for the current session, newest first.
   *  Tie-broken by version so versions of one artifact stay ordered
   *  even when their timestamps are equal or missing. */
  readonly artifacts = computed<Artifact[]>(() =>
    Array.from(this.byKey().values()).sort(
      (a, b) =>
        b.updatedAt.localeCompare(a.updatedAt) || b.version - a.version,
    ),
  );

  readonly hasArtifacts = computed(() => this.byKey().size > 0);

  /** The artifact the side panel is showing, or null when closed.
   *
   *  Gated on the rail's owner rather than mirroring `openRef` directly:
   *  when the .docx preview claims the rail, the artifact pane must read
   *  as closed immediately, with no eviction callback to miss. `openRef`
   *  is left as-is behind the gate — it is re-set from the registry on
   *  the next open, so a stale ref can never surface. */
  readonly openArtifact = computed<OpenArtifactRef | null>(() =>
    this.dockedPane.owner() === 'artifact' ? this.openRef() : null,
  );

  /** Highest known version of an artifact, if any. */
  get(artifactId: string): Artifact | undefined {
    let latest: Artifact | undefined;
    for (const a of this.byKey().values()) {
      if (a.artifactId !== artifactId) continue;
      if (!latest || a.version > latest.version) latest = a;
    }
    return latest;
  }

  /** Every known version of an artifact, newest first. */
  versionsFor(artifactId: string): Artifact[] {
    const out: Artifact[] = [];
    for (const a of this.byKey().values()) {
      if (a.artifactId === artifactId) out.push(a);
    }
    return out.sort((x, y) => y.version - x.version);
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
    // Created or updated, surface the artifact at its newest version. Read
    // the registry (not event.version) so a stale out-of-order event can't
    // pin the panel to an older version. Reopening a past session uses
    // seedFromHydration, not this, so it never pops the panel.
    const latest = this.get(event.artifactId);
    if (latest) {
      this.openArtifactPanel({
        artifactId: latest.artifactId,
        version: latest.version,
        title: latest.title,
      });
    }
  }

  /**
   * Replay the persisted session artifact list on load. Idempotent and
   * non-clobbering: an entry already at an equal-or-higher version (from
   * a live event that raced ahead) is left untouched.
   */
  seedFromHydration(list: readonly Artifact[]): void {
    for (const a of list) this.upsert(a);
  }

  /**
   * Drop every version of an artifact from the registry.
   *
   * The conversation's inline cards and the docked panel both read this
   * registry, so one call clears both — a deleted artifact must not
   * leave a card behind that 404s when clicked. Closes the panel too if
   * it happens to be showing the artifact that just went.
   *
   * Local bookkeeping only: the caller owns the HTTP delete and must
   * have seen it succeed first. Removing optimistically would put the
   * card back on the next session load if the request failed.
   */
  remove(artifactId: string): void {
    this.byKey.update((m) => {
      const next = new Map(m);
      for (const key of m.keys()) {
        if (next.get(key)?.artifactId === artifactId) next.delete(key);
      }
      return next;
    });
    if (this.openRef()?.artifactId === artifactId) {
      this.closeArtifactPanel();
    }
  }

  /**
   * Apply a new title to every known version of an artifact.
   *
   * Every version, not just the open one, because the backend renames
   * every version row — leaving older cards on the old title would
   * invent a disagreement that does not exist on the server. The open
   * panel ref carries its own copy of the title, so it is updated too.
   */
  rename(artifactId: string, title: string): void {
    this.byKey.update((m) => {
      const next = new Map(m);
      for (const [key, artifact] of m) {
        if (artifact.artifactId === artifactId) {
          next.set(key, { ...artifact, title });
        }
      }
      return next;
    });
    const open = this.openRef();
    if (open?.artifactId === artifactId) {
      this.openRef.set({ ...open, title });
    }
  }

  openArtifactPanel(ref: OpenArtifactRef): void {
    this.openRef.set(ref);
    this.dockedPane.claim('artifact');
  }

  closeArtifactPanel(): void {
    this.openRef.set(null);
    this.dockedPane.release('artifact');
  }

  /** Clear all state — called on session change. */
  reset(): void {
    this.byKey.set(new Map());
    this.closeArtifactPanel();
  }

  private upsert(a: Artifact): void {
    const key = `${a.artifactId}#${a.version}`;
    const existing = this.byKey().get(key);
    // A later reload-hydration call for the same (id, version) must not
    // drop the live entry's precise per-turn anchor.
    if (existing?.producedByMessageId && !a.producedByMessageId) return;
    this.byKey.update((m) => {
      const next = new Map(m);
      next.set(key, a);
      return next;
    });
  }
}
