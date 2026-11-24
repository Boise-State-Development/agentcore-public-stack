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
  private readonly CONFIRM_CACHE_RESET_KEY = 'confirm-cache-reset';
  private readonly COLLAPSED_SIDEBAR_GROUPS_KEY = 'sidebar-collapsed-groups';

  readonly showTokenCount = signal(this.loadBoolean(this.SHOW_TOKEN_COUNT_KEY, false));
  readonly showDebugOutput = signal(this.loadBoolean(this.SHOW_DEBUG_OUTPUT_KEY, false));

  /**
   * Whether to ask before a change that makes an open conversation's next
   * message re-process the whole conversation (a model or effort switch).
   * Turned off by the dialog's "Don't ask again".
   */
  readonly confirmCacheReset = signal(this.loadBoolean(this.CONFIRM_CACHE_RESET_KEY, true));

  /**
   * Grid is the default: it is the one that shows an agent's artwork at a size you
   * recognise, and most people have few enough agents that density is not yet the
   * problem. List becomes the better view somewhere around a screenful.
   */
  readonly agentsViewMode = signal<ViewMode>(
    this.loadViewMode(this.AGENTS_VIEW_MODE_KEY, 'grid'),
  );

  /**
   * List is still the default here, unlike Agents — but the reason has changed
   * and is now a much closer call.
   *
   * It used to be that a grid card spent a lot of space to show the same title
   * and date a row does, because an artifact had no artwork. Grid cards now
   * carry a live scaled-down render of the artifact itself
   * (`ArtifactThumbnailComponent`), so that argument is gone: a grid card
   * shows something a row cannot.
   *
   * What keeps list the default is cost, not density. Every visible grid card
   * mints a render token and invokes the render Lambda, uncached; a list view
   * costs one request for the whole page. Defaulting everyone into the
   * expensive view is a fleet-wide change to make on purpose, with the numbers
   * in hand, rather than as a side effect of shipping previews. Revisit once
   * the render path's traffic is known.
   */
  readonly artifactsViewMode = signal<ViewMode>(
    this.loadViewMode(this.ARTIFACTS_VIEW_MODE_KEY, 'list'),
  );

  /**
   * The sidebar's conversation groups ("Pinned", "Today", "Yesterday", …) the user has
   * folded, by label. Every group starts open; a label with nothing in it today is kept,
   * so a fold survives the days its bucket is empty. Per-device, like the view modes.
   */
  readonly collapsedSidebarGroups = signal<ReadonlySet<string>>(
    this.loadStringSet(this.COLLAPSED_SIDEBAR_GROUPS_KEY),
  );

  toggleSidebarGroup(label: string): void {
    const next = new Set(this.collapsedSidebarGroups());
    if (!next.delete(label)) next.add(label);
    this.collapsedSidebarGroups.set(next);
    try {
      localStorage.setItem(this.COLLAPSED_SIDEBAR_GROUPS_KEY, JSON.stringify([...next]));
    } catch {
      // Storage full or blocked: the fold lasts for this tab only.
    }
  }

  setShowTokenCount(value: boolean): void {
    this.showTokenCount.set(value);
    localStorage.setItem(this.SHOW_TOKEN_COUNT_KEY, JSON.stringify(value));
  }

  setShowDebugOutput(value: boolean): void {
    this.showDebugOutput.set(value);
    localStorage.setItem(this.SHOW_DEBUG_OUTPUT_KEY, JSON.stringify(value));
  }

  setConfirmCacheReset(value: boolean): void {
    this.confirmCacheReset.set(value);
    localStorage.setItem(this.CONFIRM_CACHE_RESET_KEY, JSON.stringify(value));
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

  private loadStringSet(key: string): ReadonlySet<string> {
    try {
      const parsed: unknown = JSON.parse(localStorage.getItem(key) ?? '[]');
      return new Set(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []);
    } catch {
      return new Set();
    }
  }

  /** Stored raw rather than JSON-encoded, so anything unrecognised falls back. */
  private loadViewMode(key: string, defaultValue: ViewMode): ViewMode {
    const stored = localStorage.getItem(key);
    return stored === 'grid' || stored === 'list' ? stored : defaultValue;
  }
}
