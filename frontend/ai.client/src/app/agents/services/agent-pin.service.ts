import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import { AgentPinsResponse, PinnedAgent } from '../models/store.model';

/**
 * The user's pinned Agents (Marketplace D8, D9 user side — Phase 5).
 *
 * One session-wide list, because three surfaces need the same answer at once: the Pinned
 * tab renders it, every Discover row asks "is this one pinned?", and the detail page's
 * Add button is the same question for one id. A per-surface fetch would show a row as
 * unpinned in one place and pinned in another within the same second.
 *
 * **Writes update the local list before the server answers**, and roll back on failure.
 * A pin is a bookmark: the honest interaction is instant, and the cost of being wrong is
 * a row that reappears with an error message rather than anything lost.
 *
 * `pinnedIds` is a `Set` rather than a scan of the array — it is read once per rendered
 * row, and a store shelf renders a lot of rows.
 */
@Injectable({ providedIn: 'root' })
export class AgentPinService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/agents`);

  private _pins = signal<PinnedAgent[]>([]);
  private _loading = signal(false);
  private _error = signal<string | null>(null);
  private loaded = false;

  readonly pins = this._pins.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  readonly pinnedIds = computed(() => new Set(this._pins().map((pin) => pin.agentId)));

  isPinned(agentId: string): boolean {
    return this.pinnedIds().has(agentId);
  }

  /**
   * Load the effective pin list, once per session unless forced.
   *
   * A failure leaves the list empty and the error set: pins are an enhancement to every
   * surface that reads them, so a store that cannot answer "what is pinned?" still
   * browses.
   */
  async load(force = false): Promise<PinnedAgent[]> {
    if (this.loaded && !force) return this._pins();

    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(
        this.http.get<AgentPinsResponse>(`${this.baseUrl()}/pins`),
      );
      this._pins.set(response.pins ?? []);
      this.loaded = true;
      return this._pins();
    } catch (err) {
      this._error.set(this.messageFor(err, 'Failed to load your pinned agents.'));
      return [];
    } finally {
      this._loading.set(false);
    }
  }

  /** Add an Agent to the user's own set. Idempotent server-side. */
  async pin(agentId: string): Promise<void> {
    if (this.isPinned(agentId)) return;
    const previous = this._pins();
    this._error.set(null);
    try {
      const row = await firstValueFrom(
        this.http.post<PinnedAgent>(`${this.baseUrl()}/${agentId}/pin`, {}),
      );
      // Re-read rather than append the optimistic guess: the server row carries the
      // resolved publisher and icon, which the caller may not have had.
      this._pins.set([...previous.filter((pin) => pin.agentId !== agentId), row]);
      this.loaded = true;
    } catch (err) {
      this._pins.set(previous);
      this._error.set(this.messageFor(err, 'Failed to add that agent.'));
      throw err;
    }
  }

  /** Remove it, and remember the dismissal server-side (D9.3). */
  async unpin(agentId: string): Promise<void> {
    const previous = this._pins();
    this._pins.set(previous.filter((pin) => pin.agentId !== agentId));
    this._error.set(null);
    try {
      await firstValueFrom(this.http.delete(`${this.baseUrl()}/${agentId}/pin`));
    } catch (err) {
      this._pins.set(previous);
      this._error.set(this.messageFor(err, 'Failed to remove that agent.'));
      throw err;
    }
  }

  /** Toggle, for the single control that both surfaces render. */
  async toggle(agentId: string): Promise<void> {
    return this.isPinned(agentId) ? this.unpin(agentId) : this.pin(agentId);
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
