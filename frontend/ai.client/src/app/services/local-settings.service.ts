import { Injectable, signal } from '@angular/core';

/** How a collection page lays its items out. Per-device, not per-account. */
export type ViewMode = 'grid' | 'list';

/** @deprecated Use {@link ViewMode} — the type was never agent-specific. */
export type AgentsViewMode = ViewMode;

@Injectable({
  providedIn: 'root'
})
export class LocalSettingsService {
  private readonly SHOW_TOKEN_COUNT_KEY = 'show-token-count';
  private readonly SHOW_DEBUG_OUTPUT_KEY = 'show-debug-output';
  private readonly AGENTS_VIEW_MODE_KEY = 'agents-view-mode';
  private readonly ARTIFACTS_VIEW_MODE_KEY = 'artifacts-view-mode';

  readonly showTokenCount = signal(this.loadBoolean(this.SHOW_TOKEN_COUNT_KEY, false));
  readonly showDebugOutput = signal(this.loadBoolean(this.SHOW_DEBUG_OUTPUT_KEY, false));

  /**
   * Grid is the default: it is the one that shows an agent's artwork at a size you
   * recognise, and most people have few enough agents that density is not yet the
   * problem. List becomes the better view somewhere around a screenful.
   */
  readonly agentsViewMode = signal<ViewMode>(
    this.loadViewMode(this.AGENTS_VIEW_MODE_KEY, 'grid'),
  );

  /**
   * List is the default here, unlike Agents. An agent tile earns its size by
   * showing artwork you recognise; an artifact has no artwork, so a grid card
   * spends a lot of space to show the same title and date a row does. Most
   * artifacts are documents — text/markdown outnumbers text/html roughly two
   * to one in production — and a document library is something you scan, not
   * something you browse.
   */
  readonly artifactsViewMode = signal<ViewMode>(
    this.loadViewMode(this.ARTIFACTS_VIEW_MODE_KEY, 'list'),
  );

  setShowTokenCount(value: boolean): void {
    this.showTokenCount.set(value);
    localStorage.setItem(this.SHOW_TOKEN_COUNT_KEY, JSON.stringify(value));
  }

  setShowDebugOutput(value: boolean): void {
    this.showDebugOutput.set(value);
    localStorage.setItem(this.SHOW_DEBUG_OUTPUT_KEY, JSON.stringify(value));
  }

  setAgentsViewMode(value: ViewMode): void {
    this.agentsViewMode.set(value);
    localStorage.setItem(this.AGENTS_VIEW_MODE_KEY, value);
  }

  setArtifactsViewMode(value: ViewMode): void {
    this.artifactsViewMode.set(value);
    localStorage.setItem(this.ARTIFACTS_VIEW_MODE_KEY, value);
  }

  private loadBoolean(key: string, defaultValue: boolean): boolean {
    const stored = localStorage.getItem(key);
    if (stored === null) return defaultValue;
    try {
      return JSON.parse(stored) === true;
    } catch {
      return defaultValue;
    }
  }

  /** Stored raw rather than JSON-encoded, so anything unrecognised falls back. */
  private loadViewMode(key: string, defaultValue: ViewMode): ViewMode {
    const stored = localStorage.getItem(key);
    return stored === 'grid' || stored === 'list' ? stored : defaultValue;
  }
}
