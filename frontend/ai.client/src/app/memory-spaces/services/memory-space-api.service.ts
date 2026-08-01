import { Injectable, inject, computed } from '@angular/core';
import { HttpClient, HttpContext, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
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

  /**
   * Per-request options shared by every Memory Spaces call. `SUPPRESS_ERROR_TOAST`
   * opts these requests out of the global error toast.
   *
   * The reason is the kill switch. While `MEMORY_SPACES_ENABLED` is off the whole
   * surface 404s *on purpose*, so the feature can be hidden without being removed —
   * MemorySpaceService reads that 404 as "feature unavailable", clears its state and
   * drops the nav entry. But error.interceptor toasts every non-401 it sees unless a
   * request opts out, so the surface would hide itself and then announce that it had:
   * a dismissable error dialog naming an endpoint the user is not meant to know about.
   *
   * Suppressing here costs nothing for genuine failures. MemorySpaceService already
   * translates every non-404 into its own `error$` signal, which the pages render
   * inline and in context; the generic toast was only ever a duplicate of that.
   */
  private requestContext(): HttpContext {
    return new HttpContext().set(SUPPRESS_ERROR_TOAST, true);
  }

  // ---- spaces ----------------------------------------------------------

  list(): Observable<SpacesListResponse> {
    return this.http.get<SpacesListResponse>(this.baseUrl(), {
      context: this.requestContext(),
    });
  }

  create(request: CreateSpaceRequest): Observable<MemorySpaceSummary> {
    return this.http.post<MemorySpaceSummary>(this.baseUrl(), request, {
      context: this.requestContext(),
    });
  }

  get(spaceId: string): Observable<MemorySpaceDetail> {
    return this.http.get<MemorySpaceDetail>(`${this.baseUrl()}/${spaceId}`, {
      context: this.requestContext(),
    });
  }

  /** Owner deletes the space; a member drops their own grant (leave). */
  remove(spaceId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl()}/${spaceId}`, {
      context: this.requestContext(),
    });
  }

  /** The whole space as a `.zip` of raw markdown (viewer+). */
  export(spaceId: string): Observable<Blob> {
    return this.http.get(`${this.baseUrl()}/${spaceId}/export`, {
      responseType: 'blob',
      context: this.requestContext(),
    });
  }

  // ---- index (MEMORY.md) ----------------------------------------------

  updateIndex(spaceId: string, content: string): Observable<{ content: string }> {
    return this.http.put<{ content: string }>(
      `${this.baseUrl()}/${spaceId}/index`,
      { content },
      { context: this.requestContext() },
    );
  }

  // ---- entries ---------------------------------------------------------

  readEntry(spaceId: string, slug: string): Observable<EntryContent> {
    return this.http.get<EntryContent>(
      `${this.baseUrl()}/${spaceId}/entries/${encodeURIComponent(slug)}`,
      { context: this.requestContext() },
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
      { context: this.requestContext() },
    );
  }

  deleteEntry(spaceId: string, slug: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl()}/${spaceId}/entries/${encodeURIComponent(slug)}`,
      { context: this.requestContext() },
    );
  }

  listEntries(spaceId: string, type?: EntryType): Observable<{ entries: MemoryEntryRef[] }> {
    let params = new HttpParams();
    if (type) {
      params = params.set('type', type);
    }
    return this.http.get<{ entries: MemoryEntryRef[] }>(
      `${this.baseUrl()}/${spaceId}/entries`,
      { params, context: this.requestContext() },
    );
  }

  // ---- sharing ---------------------------------------------------------

  listShares(spaceId: string): Observable<MembersListResponse> {
    return this.http.get<MembersListResponse>(`${this.baseUrl()}/${spaceId}/shares`, {
      context: this.requestContext(),
    });
  }

  addShare(spaceId: string, request: ShareRequest): Observable<SpaceMember> {
    return this.http.post<SpaceMember>(`${this.baseUrl()}/${spaceId}/shares`, request, {
      context: this.requestContext(),
    });
  }

  updateShare(
    spaceId: string,
    email: string,
    permission: ShareRole,
  ): Observable<SpaceMember> {
    return this.http.patch<SpaceMember>(
      `${this.baseUrl()}/${spaceId}/shares/${encodeURIComponent(email)}`,
      { permission },
      { context: this.requestContext() },
    );
  }

  removeShare(spaceId: string, email: string): Observable<void> {
    return this.http.delete<void>(
      `${this.baseUrl()}/${spaceId}/shares/${encodeURIComponent(email)}`,
      { context: this.requestContext() },
    );
  }
}
