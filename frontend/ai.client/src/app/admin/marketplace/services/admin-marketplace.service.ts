import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AdminListingRow,
  AdminStoreFrontResponse,
  AgentCategory,
  AdminListingsResponse,
  ListingPatchRequest,
  ListingState,
  PublisherProfile,
  ReviewListingRequest,
  RoleAgentPinsResponse,
  RoleAgentPinsUpdateRequest,
} from '../models/marketplace.model';

interface PublishersResponse {
  publishers: PublisherProfile[];
}

interface CategoriesResponse {
  categories: AgentCategory[];
}

/**
 * Admin surface for the Agent Marketplace (Phases 1–2, 5).
 *
 * Mirrors `AdminSkillService`: signal-backed state plus explicit reload, so the two
 * pages can share the pending count without either owning the other's fetch.
 */
@Injectable({ providedIn: 'root' })
export class AdminMarketplaceService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/agents`);

  private _loading = signal(false);
  private _error = signal<string | null>(null);
  private _pendingCount = signal(0);

  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /** Submissions awaiting review — badges the nav so the queue stays visible (D2). */
  readonly pendingCount = this._pendingCount.asReadonly();

  /** The Review queue: everything currently awaiting a decision. */
  async loadSubmissions(): Promise<AdminListingRow[]> {
    return this.fetchListings(`${this.baseUrl()}/submissions`);
  }

  /** The Listings table: every agent that has ever been submitted. */
  async loadListings(state?: ListingState): Promise<AdminListingRow[]> {
    const url = state ? `${this.baseUrl()}/listings?state=${state}` : `${this.baseUrl()}/listings`;
    return this.fetchListings(url);
  }

  private async fetchListings(url: string): Promise<AdminListingRow[]> {
    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(this.http.get<AdminListingsResponse>(url));
      this._pendingCount.set(response.pendingCount ?? 0);
      return response.listings ?? [];
    } catch (err) {
      this._error.set(this.messageFor(err, 'Failed to load listings'));
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /** Approve a submission, or return it to the author with a reason. */
  async review(agentId: string, request: ReviewListingRequest): Promise<void> {
    await firstValueFrom(this.http.post(`${this.baseUrl()}/${agentId}/review`, request));
  }

  /**
   * Delist a published agent. A delisting, not a revocation — pins keep working and
   * conversations underway keep running.
   */
  async takedown(agentId: string, reason: string): Promise<void> {
    await firstValueFrom(this.http.post(`${this.baseUrl()}/${agentId}/takedown`, { reason }));
  }

  /** Edit presentation only; the backend 422s any behavior field. */
  async patchListing(agentId: string, patch: ListingPatchRequest): Promise<void> {
    await firstValueFrom(this.http.patch(`${this.baseUrl()}/${agentId}/listing`, patch));
  }

  async loadPublishers(): Promise<PublisherProfile[]> {
    const response = await firstValueFrom(
      this.http.get<PublishersResponse>(`${this.baseUrl()}/publishers`),
    );
    return response.publishers ?? [];
  }

  async loadCategories(): Promise<AgentCategory[]> {
    const response = await firstValueFrom(
      this.http.get<CategoriesResponse>(`${this.baseUrl()}/categories`),
    );
    return response.categories ?? [];
  }

  /**
   * Create a category. The id defaults to the label server-side and is immutable after —
   * it is half of the directory partition key, so a rename changes the label only.
   */
  async createCategory(request: {
    label: string;
    order?: number;
    enabled?: boolean;
  }): Promise<AgentCategory> {
    return firstValueFrom(
      this.http.post<AgentCategory>(`${this.baseUrl()}/categories`, request),
    );
  }

  async updateCategory(
    categoryId: string,
    changes: { label?: string; order?: number; enabled?: boolean },
  ): Promise<AgentCategory> {
    return firstValueFrom(
      this.http.patch<AgentCategory>(
        `${this.baseUrl()}/categories/${encodeURIComponent(categoryId)}`,
        changes,
      ),
    );
  }

  /** Deleting is refused server-side (409) while listings still reference the category. */
  async deleteCategory(categoryId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete(`${this.baseUrl()}/categories/${encodeURIComponent(categoryId)}`),
    );
  }

  /** The featured row, resolved in its configured order (D10). */
  async loadStoreFront(): Promise<AdminStoreFrontResponse> {
    return firstValueFrom(
      this.http.get<AdminStoreFrontResponse>(`${this.baseUrl()}/storefront`),
    );
  }

  /**
   * Replace the featured row.
   *
   * A whole-list PUT because reordering has to be atomic — the ordered array *is* the
   * record. The server refuses any id that is not published and names it, so the caller
   * surfaces the message rather than pre-validating a copy of that rule.
   */
  async saveStoreFront(agentIds: string[]): Promise<AdminStoreFrontResponse> {
    return firstValueFrom(
      this.http.put<AdminStoreFrontResponse>(`${this.baseUrl()}/storefront`, { agentIds }),
    );
  }

  /**
   * A role's default pins (D9).
   *
   * Under `/admin/roles/` because the AppRole record is the source of truth for a seed —
   * the same rule that makes a role-side grant the only thing an access check reads. The
   * response carries the D9.5 diff already applied, so nothing here re-derives it.
   */
  async loadRolePins(roleId: string): Promise<RoleAgentPinsResponse> {
    return firstValueFrom(
      this.http.get<RoleAgentPinsResponse>(
        `${this.config.appApiUrl()}/admin/roles/${encodeURIComponent(roleId)}/agent-pins`,
      ),
    );
  }

  /**
   * Replace a role's default pins, in order.
   *
   * Warnings on the returned rows do not block the save — an admin may seed an agent
   * whose author is about to publish it. What the server refuses is a list that cannot
   * mean what it says: past the ceiling, or the same agent twice.
   */
  async saveRolePins(
    roleId: string,
    request: RoleAgentPinsUpdateRequest,
  ): Promise<RoleAgentPinsResponse> {
    return firstValueFrom(
      this.http.put<RoleAgentPinsResponse>(
        `${this.config.appApiUrl()}/admin/roles/${encodeURIComponent(roleId)}/agent-pins`,
        request,
      ),
    );
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
