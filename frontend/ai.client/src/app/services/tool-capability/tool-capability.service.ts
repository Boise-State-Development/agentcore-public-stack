import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';

/**
 * One argument an MCP prompt accepts.
 *
 * A snapshot taken before the backend widened this stored bare names, and those
 * rehydrate server-side with `required: false` — so an old snapshot renders a
 * form with no required fields rather than none at all.
 */
export interface McpPromptArgument {
  name: string;
  description?: string | null;
  required: boolean;
}

/** A prompt template an MCP server exposes (`prompts/list`). */
export interface McpPrompt {
  name: string;
  title?: string | null;
  description?: string | null;
  arguments: McpPromptArgument[];
}

/**
 * One message a server composed for a prompt.
 *
 * `kind` is the MCP content type. Anything but `text` carries no body — an
 * image or a binary resource has nothing readable to show — so the UI names the
 * kind instead of rendering an empty message.
 */
export interface ResolvedPromptMessage {
  role: string;
  kind: string;
  text: string;
}

/** The result of `prompts/get`, composed live from the user's arguments. */
export interface ResolvedPrompt {
  description?: string | null;
  messages: ResolvedPromptMessage[];
  truncated: boolean;
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
/**
 * Coerce a snapshot from a backend that predates structured prompt arguments.
 *
 * The two packages deploy on separate workflows and the order between them is
 * not enforced, so a SPA can reach a backend still sending bare argument names.
 * Without this the form would render a field labelled `undefined` — a shape
 * mismatch showing up as a nonsense label rather than as an error.
 */
function normalize(snapshot: ToolCapabilities): ToolCapabilities {
  return {
    ...snapshot,
    prompts: (snapshot.prompts ?? []).map((prompt) => ({
      ...prompt,
      arguments: (prompt.arguments ?? []).map((arg) =>
        typeof arg === 'string'
          ? { name: arg as string, description: null, required: false }
          : arg,
      ),
    })),
  };
}

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
      const value = normalize(
        await firstValueFrom(this.http.get<ToolCapabilities>(this.url(toolId))),
      );
      this.entries.update((current) => ({
        ...current,
        [toolId]: { state: 'loaded', value },
      }));
    } catch {
      this.entries.update((current) => ({ ...current, [toolId]: { state: 'failed' } }));
    }
  }

  /**
   * Compose one of a server's prompts with the user's argument values.
   *
   * Deliberately uncached and never stored: the result depends on arguments
   * typed a moment ago, and for a 3LO server on the caller's own token. Each
   * call is a live `prompts/get` against the server.
   */
  async resolvePrompt(
    toolId: string,
    promptName: string,
    args: Record<string, string>,
  ): Promise<ResolvedPrompt> {
    const url =
      `${this.config.appApiUrl()}/tools/${encodeURIComponent(toolId)}` +
      `/prompts/${encodeURIComponent(promptName)}`;
    return firstValueFrom(
      this.http.post<ResolvedPrompt>(url, { arguments: args }),
    );
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
