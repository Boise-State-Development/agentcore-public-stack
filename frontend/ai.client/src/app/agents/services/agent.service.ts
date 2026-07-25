import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { AgentApiService } from './agent-api.service';
import {
  Agent,
  BindableItem,
  BindableKind,
  CreateAgentDraftRequest,
  CreateAgentRequest,
  UpdateAgentRequest,
} from '../models/agent.model';

/**
 * Signal-based state for the Agent Designer. Mirrors the assistants / memory-spaces
 * facades: private mutable signals, readonly public views, async methods that
 * round-trip through the API service and keep local state in sync.
 *
 * `accessible$` rides the list call the same way `MemorySpaceService` does — a
 * successful (even empty) list means the `AGENTS_API_ENABLED` kill switch is on;
 * a 404 flips it false so the nav entry and page hide gracefully. The bindable
 * palette is memoised per-kind (`loadBindable`) so the pickers don't re-fetch on
 * every keystroke.
 */
@Injectable({ providedIn: 'root' })
export class AgentService {
  private api = inject(AgentApiService);

  private agents = signal<Agent[]>([]);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);
  private accessible = signal<boolean | null>(null);

  readonly agents$ = this.agents.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();
  readonly accessible$ = this.accessible.asReadonly();

  private bindableCache = new Map<BindableKind, BindableItem[]>();

  async loadAgents(includeDrafts = true): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.getAgents({ includeDrafts }));
      this.agents.set(response?.agents ?? []);
      this.accessible.set(true);
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 404) {
        // Kill switch off — fail gracefully rather than surfacing an error.
        this.accessible.set(false);
        this.agents.set([]);
        return;
      }
      this.error.set(err instanceof Error ? err.message : 'Failed to load agents');
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  getAgent(id: string): Promise<Agent> {
    return firstValueFrom(this.api.getAgent(id));
  }

  createDraft(request: CreateAgentDraftRequest = {}): Promise<Agent> {
    return firstValueFrom(this.api.createDraft(request));
  }

  async createAgent(request: CreateAgentRequest): Promise<Agent> {
    this.error.set(null);
    const agent = await firstValueFrom(this.api.createAgent(request));
    this.agents.update((current) => [agent, ...current]);
    return agent;
  }

  async updateAgent(id: string, request: UpdateAgentRequest): Promise<Agent> {
    this.error.set(null);
    const agent = await firstValueFrom(this.api.updateAgent(id, request));
    this.agents.update((current) => current.map((a) => (a.agentId === id ? agent : a)));
    return agent;
  }

  async deleteAgent(id: string): Promise<void> {
    this.error.set(null);
    await firstValueFrom(this.api.deleteAgent(id));
    this.agents.update((current) => current.filter((a) => a.agentId !== id));
  }

  /**
   * Merge a partial update into one cached agent, in place.
   *
   * For changes that happen outside the Designer's own write path — a marketplace
   * submission or withdrawal, which returns a listing rather than a whole Agent. The
   * alternative is reloading the list to observe one field, which discards nothing
   * useful but flickers every card.
   */
  patchAgent(id: string, patch: Partial<Agent>): void {
    this.agents.update((current) =>
      current.map((a) => (a.agentId === id ? { ...a, ...patch } : a)),
    );
  }

  /**
   * Fetch (and memoise) the RBAC-filtered bindable palette for a kind. Returns
   * `[]` on failure so a picker degrades to "nothing available" rather than
   * breaking the form. Pass `force` to bypass the cache after a mutation elsewhere.
   */
  async loadBindable(kind: BindableKind, force = false): Promise<BindableItem[]> {
    if (!force && this.bindableCache.has(kind)) {
      return this.bindableCache.get(kind)!;
    }
    try {
      const response = await firstValueFrom(this.api.getBindable(kind));
      const items = response?.items ?? [];
      this.bindableCache.set(kind, items);
      return items;
    } catch {
      return [];
    }
  }
}
