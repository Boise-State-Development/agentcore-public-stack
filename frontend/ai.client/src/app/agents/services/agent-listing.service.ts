import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { AgentApiService } from './agent-api.service';
import { AgentService } from './agent.service';
import { AgentStoreService } from './agent-store.service';
import {
  AgentCategory,
  AgentListingBlock,
  ListingPreflight,
  ListingSubmissionResponse,
  SubmitListingRequest,
} from '../models/store.model';

/**
 * The author's half of the publication flow (D2, D7, D13).
 *
 * Phases 1–3 shipped `POST /agents/{id}/listing/submit`, `DELETE /agents/{id}/listing`
 * and the whole admin review console — but nothing in the SPA called the author's two
 * routes, so an agent could not reach the store without a hand-rolled request. This is
 * the missing caller.
 *
 * `available` is the kill switch, observed rather than assumed: the marketplace routes
 * 404 as a set when `AGENT_MARKETPLACE_ENABLED=false`, and the category load is the
 * cheapest probe of that. Until it resolves, the value is `null` — "not known yet" —
 * so the card shows no publication affordance rather than one that will error on click.
 * Badges are unaffected: they render from `agent.listing`, which the list read already
 * carries, and a listing that exists is worth naming whether or not new ones may be made.
 */
@Injectable({ providedIn: 'root' })
export class AgentListingService {
  private api = inject(AgentApiService);
  private agents = inject(AgentService);
  private store = inject(AgentStoreService);

  private _categories = signal<AgentCategory[]>([]);
  private _available = signal<boolean | null>(null);

  /**
   * Whether the probe reached a durable answer. Success and a 404 both settle it — the
   * kill switch does not flip mid-session — but a transient failure does not, so one
   * bad request does not hide publication for the rest of the session.
   */
  private settled = false;

  readonly categories = this._categories.asReadonly();
  readonly available = this._available.asReadonly();

  /**
   * Load the category set the submit dialog offers, once per session.
   *
   * Only enabled categories come back — `store_front` filters them — which is the
   * behaviour the picker wants: a disabled category still holds its existing listings
   * but accepts no new ones, and offering it would produce a 400 on submit.
   */
  async loadCategories(force = false): Promise<AgentCategory[]> {
    if (!force && this.settled) {
      return this._categories();
    }
    try {
      const front = await this.store.loadStoreFront();
      this._categories.set(front.categories ?? []);
      this._available.set(true);
      this.settled = true;
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      // 404 is the kill switch behaving as designed (the routes act unmounted), and it
      // is a durable answer. Any other failure is an outage: hide the control for now —
      // submitting would fail too — but ask again rather than write the feature off.
      if (status === 404) {
        this.settled = true;
      } else {
        console.error('Error loading agent store categories:', err);
      }
      this._categories.set([]);
      this._available.set(false);
    }
    return this._categories();
  }

  /** The D7 checks, without transitioning. Owner only. */
  preflight(agentId: string): Promise<ListingPreflight> {
    return firstValueFrom(this.api.getListingPreflight(agentId));
  }

  /** Submit for review, and reflect the new state on the author's card immediately. */
  async submit(agentId: string, request: SubmitListingRequest): Promise<ListingSubmissionResponse> {
    const response = await firstValueFrom(this.api.submitListing(agentId, request));
    this.agents.patchAgent(agentId, { listing: response.listing });
    return response;
  }

  /**
   * Unpublish or withdraw (D7.3).
   *
   * A delisting, not a recall: it returns the listing to `private` and revokes nothing
   * that already happened. The confirmation copy says so — see the caller.
   */
  async withdraw(agentId: string): Promise<AgentListingBlock> {
    const listing = await firstValueFrom(this.api.withdrawListing(agentId));
    this.agents.patchAgent(agentId, { listing });
    return listing;
  }
}
