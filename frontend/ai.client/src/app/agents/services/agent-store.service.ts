import { Injectable, inject, computed, signal } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  AgentCategory,
  AgentListing,
  AgentStoreFrontResponse,
  AgentStoreResponse,
  CategoryShelf,
} from '../models/store.model';

/**
 * The user-facing marketplace read (Phase 2).
 *
 * Discover renders per-category shelves, so the load is one store-front call for the
 * header plus one call per enabled category. That maps 1:1 onto the backend's single
 * partition-per-category GSI query — the alternative (one big call, sliced client-side)
 * would page badly and hide empty categories only after fetching them.
 */
@Injectable({ providedIn: 'root' })
export class AgentStoreService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/agents/store`);

  private _loading = signal(false);
  private _error = signal<string | null>(null);

  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /** The browse header: the featured row plus the categories to render. */
  async loadStoreFront(): Promise<AgentStoreFrontResponse> {
    return firstValueFrom(this.http.get<AgentStoreFrontResponse>(`${this.baseUrl()}/front`));
  }

  /** One category's shelf, newest-first. */
  async browseCategory(categoryId: string, limit = 12): Promise<AgentListing[]> {
    const params = new HttpParams().set('category', categoryId).set('limit', String(limit));
    const response = await firstValueFrom(
      this.http.get<AgentStoreResponse>(this.baseUrl(), { params }),
    );
    return response.listings ?? [];
  }

  /** Everything, newest-first across categories. Used for search. */
  async browseAll(limit = 100): Promise<AgentListing[]> {
    const params = new HttpParams().set('limit', String(limit));
    const response = await firstValueFrom(
      this.http.get<AgentStoreResponse>(this.baseUrl(), { params }),
    );
    return response.listings ?? [];
  }

  /**
   * Load the whole Discover view: the header, then every category's shelf in parallel.
   *
   * Empty categories are dropped rather than rendered as empty headings — D10 says
   * empty categories auto-hide, and a store whose shelves are mostly labels reads as
   * broken rather than as new.
   */
  async loadDiscover(): Promise<{ featured: AgentListing[]; shelves: CategoryShelf[] }> {
    this._loading.set(true);
    this._error.set(null);
    try {
      const front = await this.loadStoreFront();
      const shelves = await Promise.all(
        (front.categories ?? []).map(async (category: AgentCategory) => ({
          category,
          listings: await this.browseCategory(category.id),
        })),
      );
      return {
        featured: front.featured ?? [],
        shelves: shelves.filter((shelf) => shelf.listings.length > 0),
      };
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this._error.set(typeof detail === 'string' ? detail : 'Failed to load the agent store.');
      throw err;
    } finally {
      this._loading.set(false);
    }
  }
}
