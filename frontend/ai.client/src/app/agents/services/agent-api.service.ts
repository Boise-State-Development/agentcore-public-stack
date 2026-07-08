import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  Agent,
  AgentsListResponse,
  AgentSharesResponse,
  BindableKind,
  BindableListResponse,
  CreateAgentDraftRequest,
  CreateAgentRequest,
  UpdateAgentRequest,
} from '../models/agent.model';
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
}
