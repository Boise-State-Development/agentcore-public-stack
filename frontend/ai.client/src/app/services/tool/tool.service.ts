import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { makeScopedToolId } from '../../shared/utils/scoped-tool-id';
/**
 * Tool category enum
 */
export type ToolCategory =
  | 'search'
  | 'data'
  | 'visualization'
  | 'document'
  | 'code'
  | 'browser'
  | 'utility'
  | 'research'
  | 'finance'
  | 'gateway'
  | 'custom';

/**
 * Tool protocol enum
 */
export type ToolProtocol = 'local' | 'aws_sdk' | 'mcp' | 'mcp_external' | 'a2a';

/**
 * Tool status enum
 */
export type ToolStatus = 'active' | 'deprecated' | 'disabled' | 'coming_soon';

/**
 * One tool exposed by an MCP server, for per-tool enablement. `enabled` is the
 * user's effective state for this individual tool.
 */
export interface ServerTool {
  name: string;
  description?: string | null;
  needsApproval?: boolean;
  enabled: boolean;
}

/**
 * Tool with user access and preference info
 */
export interface Tool {
  toolId: string;
  displayName: string;
  description: string;
  category: ToolCategory;
  icon: string | null;
  protocol: ToolProtocol;
  status: ToolStatus;
  grantedBy: string[];
  enabledByDefault: boolean;
  userEnabled: boolean | null;
  isEnabled: boolean;
  /**
   * For MCP-server tools, the individual tools the server exposes. Empty for
   * non-MCP tools or servers whose tools are discovered live.
   */
  serverTools?: ServerTool[];
}

/**
 * Response from GET /tools
 */
export interface ToolsResponse {
  tools: Tool[];
  categories: string[];
  appRolesApplied: string[];
}

/**
 * Request body for PUT /tools/preferences
 */
export interface ToolPreferencesRequest {
  preferences: Record<string, boolean>;
}

/**
 * Service for managing user tool access and preferences.
 *
 * Replaces the hardcoded ToolSettingsService with API-driven approach.
 */
@Injectable({
  providedIn: 'root'
})
export class ToolService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/tools`);

  // Internal state signals
  private _tools = signal<Tool[]>([]);
  private _loading = signal(false);
  private _error = signal<string | null>(null);
  private _appRolesApplied = signal<string[]>([]);
  private _initialized = signal(false);

  // Agent Designer: when the active conversation is bound to an Agent that binds
  // tools, the picker is locked to exactly that set — the backend governs the
  // toolset at invocation regardless of the client, so a free-select picker would
  // be dishonest. Holds the bound tool ids, or null when not agent-bound.
  private readonly _agentLockedToolIds = signal<string[] | null>(null);

  // Public readonly signals
  readonly tools = this._tools.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly appRolesApplied = this._appRolesApplied.asReadonly();
  readonly initialized = this._initialized.asReadonly();

  /** True when the toolset is dictated by the active Agent and toggles are locked. */
  readonly agentLocked = computed(() => this._agentLockedToolIds() !== null);

  constructor() {
    // Load tools on initialization (similar to ModelService pattern)
    this.loadTools().catch(err => {
      console.error('Failed to load tools on initialization:', err);
    });
  }

  // Computed signals
  readonly enabledTools = computed(() =>
    this._tools().filter(t => t.isEnabled)
  );

  /**
   * Tool ids to send to the agent. A server with a per-tool selection emits
   * scoped ids (`toolId::name`) for its enabled tools; a fully-enabled server
   * (or a tool with no sub-tools) emits its bare id.
   */
  readonly enabledToolIds = computed(() => {
    // Agent-bound: the Agent's tools are the effective set (the backend enforces
    // the same, replace semantics). Toggling is disabled, so nothing else feeds in.
    const locked = this._agentLockedToolIds();
    if (locked !== null) {
      return [...locked];
    }
    const ids: string[] = [];
    for (const tool of this._tools()) {
      const subs = tool.serverTools ?? [];
      if (subs.length === 0) {
        if (tool.isEnabled) {
          ids.push(tool.toolId);
        }
        continue;
      }
      const enabled = subs.filter(s => s.enabled);
      if (enabled.length === subs.length) {
        ids.push(tool.toolId);
      } else {
        for (const s of enabled) {
          ids.push(makeScopedToolId(tool.toolId, s.name));
        }
      }
    }
    return ids;
  });

  readonly enabledCount = computed(() => {
    const locked = this._agentLockedToolIds();
    if (locked !== null) {
      return locked.length;
    }
    return this.enabledTools().length;
  });

  /**
   * The tools the picker should render. Agent-locked → only the bound tools
   * (the agent dictates a fixed set, so hide the rest rather than show a long
   * greyed list); otherwise every accessible tool.
   */
  readonly visibleTools = computed(() => {
    const locked = this._agentLockedToolIds();
    if (locked !== null) {
      return this._tools().filter(t => locked.includes(t.toolId));
    }
    return this._tools();
  });

  /**
   * Whether a tool row should render as ON. Agent-locked → membership in the
   * bound set (so greyed toggles honestly show the Agent's toolset, not the
   * user's underlying prefs); otherwise the user's own enabled state.
   */
  isToolShownEnabled(tool: Tool): boolean {
    const locked = this._agentLockedToolIds();
    if (locked !== null) {
      return locked.includes(tool.toolId);
    }
    return tool.isEnabled;
  }

  /**
   * Whether an MCP sub-tool row should render as ON. Agent-locked → follows the
   * server's shown state (agents bind whole tools, so all subs match); otherwise
   * the sub-tool's own enabled flag.
   */
  isSubToolShownEnabled(tool: Tool, sub: { enabled: boolean }): boolean {
    if (this._agentLockedToolIds() !== null) {
      return this.isToolShownEnabled(tool);
    }
    return sub.enabled;
  }

  /** Lock the picker to an Agent's bound tools (Agent Designer). */
  lockToAgentTools(toolIds: string[]): void {
    this._agentLockedToolIds.set([...toolIds]);
  }

  /** Release an Agent tool lock. */
  clearAgentLock(): void {
    this._agentLockedToolIds.set(null);
  }

  readonly toolsByCategory = computed(() => {
    const grouped = new Map<string, Tool[]>();
    for (const tool of this._tools()) {
      const list = grouped.get(tool.category) || [];
      list.push(tool);
      grouped.set(tool.category, list);
    }
    return grouped;
  });

  readonly categories = computed(() =>
    [...new Set(this._tools().map(t => t.category))].sort()
  );

  /**
   * Fetch available tools for the current user.
   * Should be called on app init or after login.
   */
  async loadTools(): Promise<void> {
    if (this._loading()) return;

    this._loading.set(true);
    this._error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.get<ToolsResponse>(`${this.baseUrl()}/`)
      );

      this._tools.set(response.tools);
      this._appRolesApplied.set(response.appRolesApplied);
      this._initialized.set(true);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load tools';
      this._error.set(message);
      console.error('Tool load error:', err);
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Toggle a tool's enabled state. For an MCP server with per-tool entries this
   * toggles the whole server (every tool), authoritatively overriding any prior
   * per-tool selection.
   */
  async toggleTool(toolId: string): Promise<void> {
    // Agent-locked: the toolset is dictated by the Agent; ignore toggles.
    if (this._agentLockedToolIds() !== null) return;
    const tool = this._tools().find(t => t.toolId === toolId);
    if (!tool) return;

    const subs = tool.serverTools ?? [];
    const newState = !tool.isEnabled;

    if (subs.length > 0) {
      // Whole-server toggle: set the server-level default AND every known tool
      // so the new state wins over any lingering per-tool preference.
      const prefs: Record<string, boolean> = { [toolId]: newState };
      for (const s of subs) {
        prefs[makeScopedToolId(toolId, s.name)] = newState;
      }
      this._tools.update(tools =>
        tools.map(t =>
          t.toolId === toolId
            ? {
                ...t,
                isEnabled: newState,
                userEnabled: newState,
                serverTools: (t.serverTools ?? []).map(s => ({ ...s, enabled: newState })),
              }
            : t
        )
      );
      try {
        await this.savePreferences(prefs);
      } catch (err) {
        this._tools.update(tools => tools.map(t => (t.toolId === toolId ? tool : t)));
        throw err;
      }
      return;
    }

    // Optimistic update (tool with no sub-tools)
    this._tools.update(tools =>
      tools.map(t =>
        t.toolId === toolId
          ? { ...t, isEnabled: newState, userEnabled: newState }
          : t
      )
    );

    try {
      await this.savePreferences({ [toolId]: newState });
    } catch (err) {
      // Revert on error
      this._tools.update(tools =>
        tools.map(t =>
          t.toolId === toolId
            ? { ...t, isEnabled: tool.isEnabled, userEnabled: tool.userEnabled }
            : t
        )
      );
      throw err;
    }
  }

  /**
   * Toggle a single tool of an MCP server (per-tool enablement). The server's
   * `isEnabled` becomes "any tool enabled".
   */
  async toggleServerTool(toolId: string, name: string): Promise<void> {
    // Agent-locked: the toolset is dictated by the Agent; ignore toggles.
    if (this._agentLockedToolIds() !== null) return;
    const tool = this._tools().find(t => t.toolId === toolId);
    const sub = tool?.serverTools?.find(s => s.name === name);
    if (!tool || !sub) return;

    const newState = !sub.enabled;

    this._tools.update(tools =>
      tools.map(t => {
        if (t.toolId !== toolId) return t;
        const serverTools = (t.serverTools ?? []).map(s =>
          s.name === name ? { ...s, enabled: newState } : s
        );
        return { ...t, serverTools, isEnabled: serverTools.some(s => s.enabled) };
      })
    );

    try {
      await this.savePreferences({ [makeScopedToolId(toolId, name)]: newState });
    } catch (err) {
      // Revert on error
      this._tools.update(tools => tools.map(t => (t.toolId === toolId ? tool : t)));
      throw err;
    }
  }

  /**
   * Discover an MCP server's tools live and attach them as per-tool entries.
   * New entries default to the server's current enabled state.
   */
  async discoverServerTools(toolId: string): Promise<void> {
    const res = await firstValueFrom(
      this.http.post<{ tools: { name: string; description?: string | null }[] }>(
        `${this.baseUrl()}/${toolId}/discover`,
        {}
      )
    );
    this._tools.update(tools =>
      tools.map(t =>
        t.toolId === toolId
          ? {
              ...t,
              serverTools: res.tools.map(d => ({
                name: d.name,
                description: d.description,
                enabled: t.isEnabled,
              })),
            }
          : t
      )
    );
  }

  /**
   * Enable a specific tool.
   */
  async enableTool(toolId: string): Promise<void> {
    const tool = this._tools().find(t => t.toolId === toolId);
    if (!tool || tool.isEnabled) return;

    await this.toggleTool(toolId);
  }

  /**
   * Disable a specific tool.
   */
  async disableTool(toolId: string): Promise<void> {
    const tool = this._tools().find(t => t.toolId === toolId);
    if (!tool || !tool.isEnabled) return;

    await this.toggleTool(toolId);
  }

  /**
   * Save multiple tool preferences at once.
   */
  async savePreferences(preferences: Record<string, boolean>): Promise<void> {
    await firstValueFrom(
      this.http.put(`${this.baseUrl()}/preferences`, { preferences })
    );

    // Update local state
    this._tools.update(tools =>
      tools.map(t => {
        const newEnabled = preferences[t.toolId];
        if (newEnabled !== undefined) {
          return { ...t, isEnabled: newEnabled, userEnabled: newEnabled };
        }
        return t;
      })
    );
  }

  /**
   * Get a tool by ID.
   */
  getTool(toolId: string): Tool | undefined {
    return this._tools().find(t => t.toolId === toolId);
  }

  /**
   * Check if a tool is enabled.
   */
  isToolEnabled(toolId: string): boolean {
    const tool = this.getTool(toolId);
    return tool?.isEnabled ?? false;
  }

  /**
   * Get the list of enabled tool IDs (for non-signal contexts).
   */
  getEnabledToolIds(): string[] {
    return this.enabledToolIds();
  }

  /**
   * Reload tools from the server.
   */
  async reload(): Promise<void> {
    this._initialized.set(false);
    await this.loadTools();
  }
}
