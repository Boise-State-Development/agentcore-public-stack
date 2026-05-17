import { Injectable, computed, inject, signal } from '@angular/core';
import { SidenavService } from '../../../services/sidenav/sidenav.service';
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
 */
@Injectable({ providedIn: 'root' })
export class ArtifactStateService {
  private readonly sidenav = inject(SidenavService);

  private readonly byKey = signal<Map<string, Artifact>>(new Map());
  private readonly openRef = signal<OpenArtifactRef | null>(null);

  /** Sidenav collapsed state captured the moment the panel opened, so a
   *  user who had the nav open isn't left with it collapsed after they
   *  close the artifact (and one who had it collapsed keeps it that way). */
  private navWasCollapsed = false;

  /** User-controlled docked pane width in px. Shared with the layout
   *  (content padding + fixed footer/topnav offset) via a CSS var so the
   *  chat never ends up under the pane. Survives open/close within a
   *  session (it's a root singleton); resets on full reload. */
  private static readonly MIN_PANE_WIDTH = 360;
  private static readonly MAX_PANE_WIDTH = 1200;
  private static readonly DEFAULT_PANE_WIDTH = 672; // 42rem, prior fixed size
  private readonly paneWidthSignal = signal(
    ArtifactStateService.DEFAULT_PANE_WIDTH,
  );

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

  /** The artifact the side panel is showing, or null when closed. */
  readonly openArtifact = this.openRef.asReadonly();

  /** Current docked pane width in px (clamped). */
  readonly paneWidth = this.paneWidthSignal.asReadonly();

  get paneWidthMin(): number {
    return ArtifactStateService.MIN_PANE_WIDTH;
  }

  get paneWidthMax(): number {
    return ArtifactStateService.MAX_PANE_WIDTH;
  }

  /** Set the docked pane width, clamped to the allowed range. The caller
   *  is responsible for any viewport-relative ceiling (the service only
   *  knows absolute bounds, not the window size). */
  setPaneWidth(px: number): void {
    const clamped = Math.min(
      ArtifactStateService.MAX_PANE_WIDTH,
      Math.max(ArtifactStateService.MIN_PANE_WIDTH, Math.round(px)),
    );
    this.paneWidthSignal.set(clamped);
  }

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

  openArtifactPanel(ref: OpenArtifactRef): void {
    // Collapse the side nav to make room for the docked pane. Only capture
    // the prior state on a genuine closed -> open transition: switching
    // between artifacts while the panel is already open must not overwrite
    // it (the nav is collapsed by us at that point).
    if (this.openRef() === null) {
      this.navWasCollapsed = this.sidenav.isCollapsed();
      this.sidenav.collapse();
    }
    this.openRef.set(ref);
  }

  closeArtifactPanel(): void {
    if (this.openRef() === null) return;
    this.openRef.set(null);
    this.restoreNav();
  }

  /** Clear all state — called on session change. */
  reset(): void {
    this.byKey.set(new Map());
    if (this.openRef() !== null) {
      this.openRef.set(null);
      this.restoreNav();
    }
  }

  /** Return the side nav to whatever state it was in before the panel
   *  opened. If the user had it collapsed already, leave it collapsed. */
  private restoreNav(): void {
    if (!this.navWasCollapsed) this.sidenav.expand();
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
