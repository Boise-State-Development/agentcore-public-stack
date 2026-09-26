import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { makeScopedToolId } from '../../shared/utils/scoped-tool-id';
import { ToggleOptions } from '../toggle-options';

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
  /** An admin pinned this individual tool: always on, not user-togglable. */
  alwaysOn?: boolean;
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
  /**
   * What to do instead of this tool, set by an admin on a non-`active` tool
   * (e.g. 'Replaced by Canvas for Faculty'). Absent on an older backend and on
   * every active tool, so every consumer must treat it as optional.
   */
  retirementNote?: string | null;
  /** ISO date (YYYY-MM-DD) this tool stops working. Same optionality. */
  retiresOn?: string | null;
  grantedBy: string[];
  enabledByDefault: boolean;
  userEnabled: boolean | null;
  isEnabled: boolean;
  /**
   * An admin pinned this tool: the backend unions it into every turn and the
   * picker must render it locked. Optional on the wire so an older backend
   * (which omits it) reads `undefined` -> falsy, i.e. today's unlocked toggle
   * — see docs/specs/admin-always-on-tools.md §10.1.
   *
   * Already scoped to THIS user: the backend only sets it when the caller's
   * roles grant the tool, because always-on enables and never grants.
   */
  alwaysOn?: boolean;
  /**
   * OAuth provider this tool needs the user to connect before it will work,
   * or null/absent when it needs no per-user consent. Mirrors the catalog's
   * `requiresOauthProvider`.
   */
  requiresOauthProvider?: string | null;
  /**
   * For MCP-server tools, the individual tools the server exposes. Empty for
   * non-MCP tools or servers whose tools are discovered live.
   */
  serverTools?: ServerTool[];
}

/**
 * An administrator has marked this tool non-`active` — it is being retired and
 * must not be newly enabled. Existing selections are untouched: the backend
 * still grants it, still builds its client, and an Agent that binds it still
 * runs. This is a picker rule, never an access decision
 * (docs/specs/mcp-server-retirement.md §7). The predicate and the date/copy
 * helpers live in `shared/utils/retirement.ts`, shared with model retirement.
 */
import { isRetiring } from '../../shared/utils/retirement';
export { formatRetiresOn, isRetiring, retirementDetail } from '../../shared/utils/retirement';
export type { RetirementInfo } from '../../shared/utils/retirement';

/**
 * Response from GET /tools
 */
export interface ToolsResponse {
  tools: Tool[];
  categories: string[];
  appRolesApplied: string[];
}

/** Re-exported so existing `tool.service` import sites keep working. */
export type { ToggleOptions };

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

  /**
   * The load currently in flight, so a second caller *joins* it instead of
   * being told "already loading" and continuing with an empty list. The
   * constructor starts this load at bootstrap, which means a message sent a
   * second or two after page load could otherwise be assembled from an empty
   * tool list — and the next turn, with the real list, would rewrite the
   * `toolConfig` half of the cacheable prefix.
   */
  private _inflight: Promise<void> | null = null;

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
    // Join an in-flight load rather than returning early: callers await this to
    // know the list is settled.
    if (this._inflight) return this._inflight;

    const inflight = this.fetchTools();
    this._inflight = inflight;
    try {
      await inflight;
    } finally {
      this._inflight = null;
    }
  }

  private async fetchTools(): Promise<void> {
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
   *
   * `respectAgentLock` defaults to true, which is the conversation-scoped
   * behaviour the composer drawer depends on. Global surfaces (Customize) pass
   * `false`: an Agent lock is a fact about one conversation, and this service is
   * a root singleton whose lock outlives the session view that set it (see
   * `docs/specs/customize-surface.md` §"The agent-lock seam"). Honouring it off
   * the conversation would make the user's own preference page silently inert.
   */
  async toggleTool(toolId: string, options?: ToggleOptions): Promise<void> {
    // Agent-locked: the toolset is dictated by the Agent; ignore toggles.
    if ((options?.respectAgentLock ?? true) && this._agentLockedToolIds() !== null) return;
    const tool = this._tools().find(t => t.toolId === toolId);
    if (!tool) return;
    // Admin-pinned: the backend unions it in regardless, so a toggle here
    // could only produce a picker that disagrees with the turn. The UI renders
    // the control disabled; this is the guard behind it, for the keyboard and
    // programmatic paths that never see a disabled attribute.
    if (tool.alwaysOn) return;
    // Retiring: an administrator has marked it non-`active`. It can be turned
    // OFF but not ON, so a user who already has it keeps it while nobody new
    // picks it up. Same backstop role as the guard above — the UI disables the
    // control in that one direction. See docs/specs/mcp-server-retirement.md §7.
    if (isRetiring(tool) && !tool.isEnabled) return;

    const subs = tool.serverTools ?? [];
    const newState = !tool.isEnabled;

    if (subs.length > 0) {
      // Whole-server toggle: set the server-level default AND every known tool
      // so the new state wins over any lingering per-tool preference.
      const prefs: Record<string, boolean> = { [toolId]: newState };
      for (const s of subs) {
        // A pinned tool of an otherwise-togglable server stays on when the
        // user switches the server off. Sending `false` for it would be
        // dropped by the backend guard anyway (D6) — not sending it keeps the
        // picker's optimistic state and the saved state in agreement.
        if (s.alwaysOn) continue;
        prefs[makeScopedToolId(toolId, s.name)] = newState;
      }
      this._tools.update(tools =>
        tools.map(t =>
          t.toolId === toolId
            ? {
                ...t,
                // A server with a pinned tool is still effectively on: the
                // row's state is "any tool enabled", and one of them cannot
                // be turned off.
                isEnabled: newState || subs.some(s => s.alwaysOn),
                userEnabled: newState,
                serverTools: (t.serverTools ?? []).map(s =>
                  s.alwaysOn ? s : { ...s, enabled: newState }
                ),
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
  async toggleServerTool(toolId: string, name: string, options?: ToggleOptions): Promise<void> {
    // Agent-locked: the toolset is dictated by the Agent; ignore toggles.
    if ((options?.respectAgentLock ?? true) && this._agentLockedToolIds() !== null) return;
    const tool = this._tools().find(t => t.toolId === toolId);
    const sub = tool?.serverTools?.find(s => s.name === name);
    if (!tool || !sub) return;
    // Pinned individually, or by the whole server being pinned.
    if (sub.alwaysOn || tool.alwaysOn) return;
    // Retiring, and the server is entirely off: turning one of its tools on
    // would be adopting the server. Gated on the SERVER's state, not the
    // sub-tool's, because access — and retirement — are properties of the
    // server (`can_access_tool` keys on the base id). Narrowing a server the
    // user already has stays open.
    if (isRetiring(tool) && !tool.isEnabled) return;

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
   * Resolve once the tool list has settled, starting the load if nothing has.
   *
   * The chat send path awaits this so a turn sent before the constructor's
   * `/tools/` fetch returns still carries the same tools a later turn would.
   * Identical `enabled_tools` on turn 1 and turn 2 is what keeps the cacheable
   * `toolConfig` prefix stable across a session.
   *
   * Never rejects: a failed load leaves the list empty, which is the state the
   * send path already tolerates. It is a no-op once loaded, and joins the
   * in-flight request when one is already running.
   */
  async ensureLoaded(): Promise<void> {
    if (this._initialized()) return;
    await this.loadTools().catch(() => undefined);
  }

  /**
   * Reload tools from the server.
   */
  async reload(): Promise<void> {
    this._initialized.set(false);
    await this.loadTools();
  }
}
