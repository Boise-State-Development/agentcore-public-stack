import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  Agent,
  AgentRunnability,
  AgentsListResponse,
  AgentSharesResponse,
  BindableKind,
  BindableListResponse,
  CreateAgentDraftRequest,
  CreateAgentRequest,
  UpdateAgentRequest,
} from '../models/agent.model';
import {
  AgentListingBlock,
  ListingPreflight,
  ListingSubmissionResponse,
  SubmitListingRequest,
} from '../models/store.model';
import {
  ShareAssistantRequest,
  UnshareAssistantRequest,
  UpdateSharePermissionRequest,
} from '../../assistants/models/assistant.model';

/**
 * Thin HTTP client over the `/agents` surface (Agent Designer). Mirrors
 * `AssistantApiService`: raw `Observable`s, base URL from `ConfigService`, the
 * signal facade (`AgentService`) owns state + error translation. Share requests
 * reuse the assistants share DTOs — the records are the same (agentId == assistantId).
 */
@Injectable({ providedIn: 'root' })
export class AgentApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/agents`);

  createDraft(request: CreateAgentDraftRequest = {}): Observable<Agent> {
    return this.http.post<Agent>(`${this.baseUrl()}/draft`, request);
  }

  createAgent(request: CreateAgentRequest): Observable<Agent> {
    return this.http.post<Agent>(this.baseUrl(), request);
  }

  getAgents(params?: { includeDrafts?: boolean }): Observable<AgentsListResponse> {
    let httpParams = new HttpParams();
    if (params?.includeDrafts !== undefined) {
      httpParams = httpParams.set('include_drafts', params.includeDrafts.toString());
    }
    return this.http.get<AgentsListResponse>(this.baseUrl(), { params: httpParams });
  }

  getAgent(id: string): Observable<Agent> {
    return this.http.get<Agent>(`${this.baseUrl()}/${id}`);
  }

  /**
   * Will this Agent run for the signed-in user? (D6)
   *
   * A separate call from `getAgent` on purpose: the detail page paints identity,
   * description and starters immediately, and this answer — which fans out across the
   * viewer's model/tool/skill catalogs — resolves into the sidebar when it arrives.
   */
  getRunnability(id: string): Observable<AgentRunnability> {
    return this.http.get<AgentRunnability>(`${this.baseUrl()}/${id}/runnability`);
  }

  updateAgent(id: string, request: UpdateAgentRequest): Observable<Agent> {
    return this.http.put<Agent>(`${this.baseUrl()}/${id}`, request);
  }

  deleteAgent(id: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${id}`);
  }

  /** RBAC-filtered palette of bindable primitives of one kind (Phase 2). */
  getBindable(kind: BindableKind): Observable<BindableListResponse> {
    const params = new HttpParams().set('kind', kind);
    return this.http.get<BindableListResponse>(`${this.baseUrl()}/bindable`, { params });
  }

  shareAgent(id: string, request: ShareAssistantRequest): Observable<AgentSharesResponse> {
    return this.http.post<AgentSharesResponse>(`${this.baseUrl()}/${id}/shares`, request);
  }

  unshareAgent(id: string, request: UnshareAssistantRequest): Observable<AgentSharesResponse> {
    return this.http.delete<AgentSharesResponse>(`${this.baseUrl()}/${id}/shares`, { body: request });
  }

  updateSharePermission(id: string, request: UpdateSharePermissionRequest): Observable<AgentSharesResponse> {
    return this.http.patch<AgentSharesResponse>(`${this.baseUrl()}/${id}/shares`, request);
  }

  getAgentShares(id: string): Observable<AgentSharesResponse> {
    return this.http.get<AgentSharesResponse>(`${this.baseUrl()}/${id}/shares`);
  }

  // ── marketplace, the author's half (D2/D7) ─────────────────────────────────────
  /** The D7 answers before the author commits: skill exposure and any block. */
  getListingPreflight(id: string): Observable<ListingPreflight> {
    return this.http.get<ListingPreflight>(`${this.baseUrl()}/${id}/listing/preflight`);
  }

  submitListing(id: string, request: SubmitListingRequest): Observable<ListingSubmissionResponse> {
    return this.http.post<ListingSubmissionResponse>(
      `${this.baseUrl()}/${id}/listing/submit`,
      request,
    );
  }

  /** Unpublish or withdraw. Returns the listing at `private`; revokes nothing (D7.3). */
  withdrawListing(id: string): Observable<AgentListingBlock> {
    return this.http.delete<AgentListingBlock>(`${this.baseUrl()}/${id}/listing`);
  }
}
