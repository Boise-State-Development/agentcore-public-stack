import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AgentVersionDiff,
  AgentVersionsResponse,
  RollbackListingRequest,
  PublisherCreateRequest,
  PublisherEligibilityResponse,
  PublisherUpdateRequest,
  AdminListingRow,
  AdminQueueCounts,
  AdminReportRow,
  AdminReportsResponse,
  AdminStoreFrontResponse,
  AgentCategory,
  AdminListingsResponse,
  ListingPatchRequest,
  ListingState,
  PublisherProfile,
  ResolveReportRequest,
  ReviewListingRequest,
  RoleAgentPinsResponse,
  RoleAgentPinsUpdateRequest,
  WithdrawalDecisionRequest,
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
  private _openReportCount = signal(0);

  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /** Submissions awaiting review — badges the nav so the queue stays visible (D2). */
  readonly pendingCount = this._pendingCount.asReadonly();

  /** Problem reports awaiting triage — badges the nav alongside submissions (D10). */
  readonly openReportCount = this._openReportCount.asReadonly();

  /** The Review queue: everything currently awaiting a decision. */
  async loadSubmissions(): Promise<AdminListingRow[]> {
    return this.fetchListings(`${this.baseUrl()}/submissions`);
  }

  /**
   * What a pending submission changes against what is published (§6.1).
   *
   * Fetched per row on demand rather than with the queue: the queue is a list of decisions
   * to make, and pre-loading a diff for every row would pull every pending Agent's full
   * instructions down to render badges nobody has opened yet.
   */
  async loadDiff(agentId: string): Promise<AgentVersionDiff> {
    return firstValueFrom(
      this.http.get<AgentVersionDiff>(`${this.baseUrl()}/${agentId}/diff`),
    );
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
   * Grant or decline an author's request to pull a live listing (§5.1).
   *
   * Deliberately not folded into `review`. That call answers "may this go into the store?";
   * this one answers "may this come out?", and the two have opposite defaults — a
   * withdrawal request routed through `review` with `approve` reads as agreeing with the
   * author while actually re-publishing over their request.
   */
  async decideWithdrawal(agentId: string, request: WithdrawalDecisionRequest): Promise<void> {
    await firstValueFrom(this.http.post(`${this.baseUrl()}/${agentId}/withdrawal`, request));
  }

  /** Every snapshot this agent has, newest first — the rollback picker's source (§8). */
  async loadVersions(agentId: string): Promise<AgentVersionsResponse> {
    return firstValueFrom(
      this.http.get<AgentVersionsResponse>(`${this.baseUrl()}/${agentId}/versions`),
    );
  }

  /**
   * Repoint a published listing at an earlier snapshot (§8).
   *
   * Not a review decision: no version is cut, nothing enters the queue, and the listing
   * stays published throughout. It only changes *which* approved artifact the store serves,
   * which is why the backend refuses it on anything that is not already published.
   */
  async rollback(agentId: string, request: RollbackListingRequest): Promise<void> {
    await firstValueFrom(this.http.post(`${this.baseUrl()}/${agentId}/rollback`, request));
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

  // ── problem reports (D15) ─────────────────────────────────────────────────────
  /**
   * The Reports queue.
   *
   * ⚠️ Rows carry the reporter (D15.2) and are admin-only. Nothing fetched here may be
   * rendered on a user-facing surface, and no count derived from it may reach the store
   * front or any ordering — report volume influencing placement would make reporting a
   * way to bury a competitor's agent.
   */
  async loadReports(): Promise<AdminReportRow[]> {
    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(
        this.http.get<AdminReportsResponse>(`${this.baseUrl()}/reports`),
      );
      this._openReportCount.set(response.openCount ?? 0);
      return response.reports ?? [];
    } catch (err) {
      this._error.set(this.messageFor(err, 'Failed to load reports'));
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /** Every report ever filed on one agent — is this complaint a pattern? */
  async loadAgentReports(agentId: string): Promise<AdminReportRow[]> {
    const response = await firstValueFrom(
      this.http.get<AdminReportsResponse>(
        `${this.baseUrl()}/${encodeURIComponent(agentId)}/reports`,
      ),
    );
    return response.reports ?? [];
  }

  /**
   * Resolve or dismiss a report (D15.5).
   *
   * ⚠️ This never delists anything. A report is a note about an agent, not a state of it;
   * if one warrants a takedown, that is `takedown()` above and a separately recorded act
   * with its own author-facing reason.
   */
  async resolveReport(
    agentId: string,
    reportId: string,
    request: ResolveReportRequest,
  ): Promise<void> {
    await firstValueFrom(
      this.http.post(
        `${this.baseUrl()}/${encodeURIComponent(agentId)}/reports/${encodeURIComponent(reportId)}/resolve`,
        request,
      ),
    );
  }

  /**
   * Refresh both nav badge counts (D10).
   *
   * Its own endpoint rather than two queue loads: the badges have to be right on every
   * admin page, and fetching two full queues to render two integers would put a table
   * scan behind every click in the console. Failures are swallowed — a badge is
   * orientation, and an unreachable count must not error the shell around a working page.
   */
  async refreshQueueCounts(): Promise<void> {
    try {
      const counts = await firstValueFrom(
        this.http.get<AdminQueueCounts>(`${this.baseUrl()}/queues`),
      );
      this._pendingCount.set(counts.pendingCount ?? 0);
      this._openReportCount.set(counts.openReportCount ?? 0);
    } catch {
      // Leave whatever the last successful read said.
    }
  }

  async loadPublishers(): Promise<PublisherProfile[]> {
    const response = await firstValueFrom(
      this.http.get<PublishersResponse>(`${this.baseUrl()}/publishers`),
    );
    return response.publishers ?? [];
  }

  /**
   * Create a publisher profile (D12).
   *
   * The id is server-generated from the label, and immutable — it is what listings store,
   * so renaming a publisher must never strand the attributions pointing at it.
   */
  async createPublisher(request: PublisherCreateRequest): Promise<PublisherProfile> {
    return firstValueFrom(
      this.http.post<PublisherProfile>(`${this.baseUrl()}/publishers`, request),
    );
  }

  async updatePublisher(
    publisherId: string,
    request: PublisherUpdateRequest,
  ): Promise<PublisherProfile> {
    return firstValueFrom(
      this.http.patch<PublisherProfile>(`${this.baseUrl()}/publishers/${publisherId}`, request),
    );
  }

  /** Refused (409) while listings are attributed to it — disable instead. */
  async deletePublisher(publisherId: string): Promise<void> {
    await firstValueFrom(this.http.delete(`${this.baseUrl()}/publishers/${publisherId}`));
  }

  /**
   * Who may *propose* this publisher at submission (D12).
   *
   * ⚠️ An allowlist for the author's submit dialog only. An admin may attribute any listing
   * to any publisher regardless of it — that is how the store gets its day-one set of
   * official Agents without a staff member's personal name on them. It never appears in an
   * access check.
   */
  async loadPublisherEligibility(publisherId: string): Promise<string[]> {
    const response = await firstValueFrom(
      this.http.get<PublisherEligibilityResponse>(
        `${this.baseUrl()}/publishers/${publisherId}/eligibility`,
      ),
    );
    return response.userIds ?? [];
  }

  async savePublisherEligibility(publisherId: string, userIds: string[]): Promise<string[]> {
    const response = await firstValueFrom(
      this.http.put<PublisherEligibilityResponse>(
        `${this.baseUrl()}/publishers/${publisherId}/eligibility`,
        { userIds },
      ),
    );
    return response.userIds ?? [];
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
