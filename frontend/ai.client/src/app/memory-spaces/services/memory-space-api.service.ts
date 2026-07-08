import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  CreateSpaceRequest,
  EntryContent,
  EntryType,
  MembersListResponse,
  MemoryEntryRef,
  MemorySpaceDetail,
  MemorySpaceSummary,
  ShareRequest,
  ShareRole,
  SpaceMember,
  SpacesListResponse,
  UpsertEntryRequest,
} from '../models/memory-space.model';

/**
 * HTTP surface for the Memory Spaces user API (`/memory/spaces/*`).
 *
 * Thin and untyped-error by design (mirrors AssistantApiService): returns raw
 * Observables and leaves state, error translation, and cache upkeep to
 * MemorySpaceService. The whole surface 404s while `MEMORY_SPACES_ENABLED` is
 * off on the backend — the facade treats that as "feature unavailable".
 */
@Injectable({ providedIn: 'root' })
export class MemorySpaceApiService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/memory/spaces`);

  // ---- spaces ----------------------------------------------------------

  list(): Observable<SpacesListResponse> {
    return this.http.get<SpacesListResponse>(this.baseUrl());
  }

  create(request: CreateSpaceRequest): Observable<MemorySpaceSummary> {
    return this.http.post<MemorySpaceSummary>(this.baseUrl(), request);
  }

  get(spaceId: string): Observable<MemorySpaceDetail> {
    return this.http.get<MemorySpaceDetail>(`${this.baseUrl()}/${spaceId}`);
  }

  /** Owner deletes the space; a member drops their own grant (leave). */
  remove(spaceId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${spaceId}`);
  }

  /** The whole space as a `.zip` of raw markdown (viewer+). */
  export(spaceId: string): Observable<Blob> {
    return this.http.get(`${this.baseUrl()}/${spaceId}/export`, {
      responseType: 'blob',
    });
  }

  // ---- index (MEMORY.md) ----------------------------------------------

  updateIndex(spaceId: string, content: string): Observable<{ content: string }> {
    return this.http.put<{ content: string }>(
      `${this.baseUrl()}/${spaceId}/index`,
      { content },
    );
  }

  // ---- entries ---------------------------------------------------------

  readEntry(spaceId: string, slug: string): Observable<EntryContent> {
    return this.http.get<EntryContent>(
      `${this.baseUrl()}/${spaceId}/entries/${encodeURIComponent(slug)}`,
    );
  }

  upsertEntry(
    spaceId: string,
    slug: string,
    request: UpsertEntryRequest,
  ): Observable<MemoryEntryRef> {
    return this.http.put<MemoryEntryRef>(
      `${this.baseUrl()}/${spaceId}/entries/${encodeURIComponent(slug)}`,
      request,
    );
  }

  deleteEntry(spaceId: string, slug: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl()}/${spaceId}/entries/${encodeURIComponent(slug)}`,
    );
  }

  listEntries(spaceId: string, type?: EntryType): Observable<{ entries: MemoryEntryRef[] }> {
    let params = new HttpParams();
    if (type) {
      params = params.set('type', type);
    }
    return this.http.get<{ entries: MemoryEntryRef[] }>(
      `${this.baseUrl()}/${spaceId}/entries`,
      { params },
    );
  }

  // ---- sharing ---------------------------------------------------------

  listShares(spaceId: string): Observable<MembersListResponse> {
    return this.http.get<MembersListResponse>(`${this.baseUrl()}/${spaceId}/shares`);
  }

  addShare(spaceId: string, request: ShareRequest): Observable<SpaceMember> {
    return this.http.post<SpaceMember>(`${this.baseUrl()}/${spaceId}/shares`, request);
  }

  updateShare(
    spaceId: string,
    email: string,
    permission: ShareRole,
  ): Observable<SpaceMember> {
    return this.http.patch<SpaceMember>(
      `${this.baseUrl()}/${spaceId}/shares/${encodeURIComponent(email)}`,
      { permission },
    );
  }

  removeShare(spaceId: string, email: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl()}/${spaceId}/shares/${encodeURIComponent(email)}`,
    );
  }
}
