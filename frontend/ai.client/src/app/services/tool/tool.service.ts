import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
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
export type ToolProtocol = 'local' | 'aws_sdk' | 'mcp' | 'a2a';

/**
 * Tool status enum
 */
export type ToolStatus = 'active' | 'deprecated' | 'disabled' | 'coming_soon';

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

  // Public readonly signals
  readonly tools = this._tools.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly appRolesApplied = this._appRolesApplied.asReadonly();
  readonly initialized = this._initialized.asReadonly();

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

  readonly enabledToolIds = computed(() =>
    this.enabledTools().map(t => t.toolId)
  );

  readonly enabledCount = computed(() =>
    this.enabledTools().length
  );

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
   * Toggle a tool's enabled state.
   */
  async toggleTool(toolId: string): Promise<void> {
    const tool = this._tools().find(t => t.toolId === toolId);
    if (!tool) return;

    const newState = !tool.isEnabled;

    // Optimistic update
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
