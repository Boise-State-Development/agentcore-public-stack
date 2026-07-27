import { Injectable, signal } from '@angular/core';

/** How My Agents lays its list out. Per-device, not per-account. */
export type AgentsViewMode = 'grid' | 'list';

@Injectable({
  providedIn: 'root'
})
export class LocalSettingsService {
  private readonly SHOW_TOKEN_COUNT_KEY = 'show-token-count';
  private readonly SHOW_DEBUG_OUTPUT_KEY = 'show-debug-output';
  private readonly AGENTS_VIEW_MODE_KEY = 'agents-view-mode';

  readonly showTokenCount = signal(this.loadBoolean(this.SHOW_TOKEN_COUNT_KEY, false));
  readonly showDebugOutput = signal(this.loadBoolean(this.SHOW_DEBUG_OUTPUT_KEY, false));

  /**
   * Grid is the default: it is the one that shows an agent's artwork at a size you
   * recognise, and most people have few enough agents that density is not yet the
   * problem. List becomes the better view somewhere around a screenful.
   */
  readonly agentsViewMode = signal<AgentsViewMode>(
    this.loadViewMode(this.AGENTS_VIEW_MODE_KEY, 'grid'),
  );

  setShowTokenCount(value: boolean): void {
    this.showTokenCount.set(value);
    localStorage.setItem(this.SHOW_TOKEN_COUNT_KEY, JSON.stringify(value));
  }

  setShowDebugOutput(value: boolean): void {
    this.showDebugOutput.set(value);
    localStorage.setItem(this.SHOW_DEBUG_OUTPUT_KEY, JSON.stringify(value));
  }

  setAgentsViewMode(value: AgentsViewMode): void {
    this.agentsViewMode.set(value);
    localStorage.setItem(this.AGENTS_VIEW_MODE_KEY, value);
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
  private loadViewMode(key: string, defaultValue: AgentsViewMode): AgentsViewMode {
    const stored = localStorage.getItem(key);
    return stored === 'grid' || stored === 'list' ? stored : defaultValue;
  }
}
