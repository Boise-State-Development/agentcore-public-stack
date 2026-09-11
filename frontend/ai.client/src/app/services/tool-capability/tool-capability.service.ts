import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';

/** A prompt template an MCP server exposes (`prompts/list`). */
export interface McpPrompt {
  name: string;
  title?: string | null;
  description?: string | null;
  arguments: string[];
}

/**
 * A resource an MCP server exposes. `uriTemplate` marks entries that came from
 * `resources/templates/list` — those are patterns like
 * `canvas://courses/{course_id}/syllabus`, not readable URIs.
 */
export interface McpResource {
  uri: string;
  name?: string | null;
  description?: string | null;
  mimeType?: string | null;
  uriTemplate: boolean;
}

/**
 * What a server last told us it offers.
 *
 * `supportsPrompts` / `supportsResources` are separate from `error` on purpose:
 * a server that answers `prompts/list` with "method not found" offers no
 * prompts, which is a different fact from one we could not reach at all. The
 * two states read differently in the UI.
 */
export interface ToolCapabilities {
  toolId: string;
  prompts: McpPrompt[];
  resources: McpResource[];
  supportsPrompts: boolean;
  supportsResources: boolean;
  discoveredAt?: string | null;
  discoveredBy?: string | null;
  error?: string | null;
  truncated: boolean;
}

type Entry =
  | { state: 'loading' }
  | { state: 'loaded'; value: ToolCapabilities }
  | { state: 'failed' };

/**
 * Reads the stored capability snapshot for a tool.
 *
 * Deliberately a read of what an admin last discovered, never a live probe:
 * probing opens an MCP session per server, and a 3LO server cannot be reached
 * without a consent token the browser does not hold.
 *
 * Cached per tool for the life of the page — a snapshot only changes when an
 * admin refreshes it, so re-fetching every time the detail pane opens would be
 * a request per drill-in for an answer that rarely moves.
 */
@Injectable({ providedIn: 'root' })
export class ToolCapabilityService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly entries = signal<Record<string, Entry>>({});

  private url(toolId: string): string {
    return `${this.config.appApiUrl()}/tools/${encodeURIComponent(toolId)}/capabilities`;
  }

  /** Snapshot for a tool, or null while loading / if the read failed. */
  capabilitiesFor(toolId: string): ToolCapabilities | null {
    const entry = this.entries()[toolId];
    return entry?.state === 'loaded' ? entry.value : null;
  }

  isLoading(toolId: string): boolean {
    return this.entries()[toolId]?.state === 'loading';
  }

  hasFailed(toolId: string): boolean {
    return this.entries()[toolId]?.state === 'failed';
  }

  /**
   * Fetch once per tool. A failed read is remembered rather than retried on
   * every render — the detail pane re-reads this on each change detection, and
   * retrying there would turn one dead endpoint into a request loop.
   */
  async ensure(toolId: string): Promise<void> {
    if (!toolId || this.entries()[toolId]) return;
    this.entries.update((current) => ({ ...current, [toolId]: { state: 'loading' } }));
    try {
      const value = await firstValueFrom(
        this.http.get<ToolCapabilities>(this.url(toolId)),
      );
      this.entries.update((current) => ({
        ...current,
        [toolId]: { state: 'loaded', value },
      }));
    } catch {
      this.entries.update((current) => ({ ...current, [toolId]: { state: 'failed' } }));
    }
  }

  /** Drop a cached snapshot so the next `ensure` re-reads it. */
  invalidate(toolId: string): void {
    this.entries.update((current) => {
      if (!(toolId in current)) return current;
      const next = { ...current };
      delete next[toolId];
      return next;
    });
  }
}
