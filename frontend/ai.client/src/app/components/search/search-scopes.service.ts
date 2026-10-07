import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { AgentApiService } from '../../agents/services/agent-api.service';
import { AgentPinService } from '../../agents/services/agent-pin.service';
import { Agent } from '../../agents/models/agent.model';
import { ArtifactHttpService, LibraryArtifact } from '../../session/services/artifacts/artifact-http.service';

/** Where one scope's list is: not asked for yet, on its way, held, or failed. */
export type ScopeLoadState = 'idle' | 'loading' | 'ready' | 'error';

/** Why an agent the user did not write is in their list; `null` for their own. */
export type SearchAgentTag = 'Public' | 'Shared' | 'Draft';

/** One agent as the search dialog lists it. */
export interface SearchableAgent {
  agentId: string;
  name: string;
  /** What the filter reads besides the name: the description, or a pin's tagline. */
  description: string;
  tag: SearchAgentTag | null;
  /** A draft opens its editor; everything else opens its page. */
  draft: boolean;
}

function httpStatus(err: unknown): number | undefined {
  return (err as { status?: number } | null)?.status;
}

function fromAgent(agent: Agent): SearchableAgent {
  const draft = agent.status === 'DRAFT';
  return {
    agentId: agent.agentId,
    name: agent.name,
    description: agent.description || agent.tagline || '',
    tag: agent.isSharedWithMe ? 'Shared' : draft ? 'Draft' : null,
    draft,
  };
}

/**
 * The search dialog's Agents and Artifacts lists (conversation-search §6a,
 * "Where each scope's data comes from"): each fetched once, the first time the
 * dialog needs it in a page load, and held until the next full load. Root, so
 * closing and reopening the dialog reuses what is here; imported only by the
 * dialog, so it ships in the dialog's lazy chunk.
 *
 * Agents are the caller's own and shared ones (`GET /agents`, drafts included,
 * as the agents page lists them) plus the store agents they pinned
 * (`AgentPinService`, already session-cached), which carry the "Public" tag.
 * That is the reachable set the composer's `@` menu uses; the whole store is
 * the Discover page's job. Artifacts are `GET /artifacts/library`, the same
 * one-page response the library page filters.
 *
 * A failed fetch stays failed until `retryFailed()` (the next open), so a
 * scope that errors does not refetch on every keystroke.
 */
@Injectable({ providedIn: 'root' })
export class SearchScopesService {
  private readonly agentApi = inject(AgentApiService);
  private readonly pins = inject(AgentPinService);
  private readonly artifactHttp = inject(ArtifactHttpService);

  private readonly agentsSignal = signal<SearchableAgent[]>([]);
  private readonly agentsStateSignal = signal<ScopeLoadState>('idle');
  private readonly artifactsSignal = signal<LibraryArtifact[]>([]);
  private readonly artifactsStateSignal = signal<ScopeLoadState>('idle');

  readonly agents = this.agentsSignal.asReadonly();
  readonly agentsState = this.agentsStateSignal.asReadonly();
  readonly artifacts = this.artifactsSignal.asReadonly();
  readonly artifactsState = this.artifactsStateSignal.asReadonly();

  /** Let a scope that failed earlier in this page load try again. */
  retryFailed(): void {
    if (this.agentsStateSignal() === 'error') this.agentsStateSignal.set('idle');
    if (this.artifactsStateSignal() === 'error') this.artifactsStateSignal.set('idle');
  }

  async loadAgents(): Promise<void> {
    if (this.agentsStateSignal() !== 'idle') return;
    this.agentsStateSignal.set('loading');
    // The pin list swallows its own failure (pins are an enhancement), so only
    // the caller's own list can fail the scope.
    const [listed, pinned] = await Promise.allSettled([
      firstValueFrom(this.agentApi.getAgents({ includeDrafts: true })),
      this.pins.load(),
    ]);
    if (listed.status === 'rejected' && httpStatus(listed.reason) !== 404) {
      this.agentsStateSignal.set('error');
      return;
    }
    // 404 is the agents kill switch: no agents of one's own, not an error.
    const own = listed.status === 'fulfilled' ? (listed.value?.agents ?? []).map(fromAgent) : [];
    const ownIds = new Set(own.map(a => a.agentId));
    const pins = (pinned.status === 'fulfilled' ? pinned.value : [])
      .filter(pin => !ownIds.has(pin.agentId))
      .map<SearchableAgent>(pin => ({
        agentId: pin.agentId,
        name: pin.name,
        description: pin.tagline ?? '',
        tag: 'Public',
        draft: false,
      }));
    this.agentsSignal.set([...own, ...pins]);
    this.agentsStateSignal.set('ready');
  }

  async loadArtifacts(): Promise<void> {
    if (this.artifactsStateSignal() !== 'idle') return;
    this.artifactsStateSignal.set('loading');
    try {
      this.artifactsSignal.set(await this.artifactHttp.listLibrary());
      this.artifactsStateSignal.set('ready');
    } catch (err) {
      if (httpStatus(err) === 404) {
        this.artifactsSignal.set([]);
        this.artifactsStateSignal.set('ready');
      } else {
        this.artifactsStateSignal.set('error');
      }
    }
  }
}
