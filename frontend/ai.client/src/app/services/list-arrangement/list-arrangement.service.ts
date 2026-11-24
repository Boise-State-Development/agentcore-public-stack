import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, Signal, computed, inject, resource, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import { FEATURES } from '../features';
import {
  Arrangement,
  ArrangementSort,
  ArrangementSurface,
  DEFAULT_ARRANGEMENT,
  foldNew,
  withArranged,
  withHidden,
} from './list-arrangement.models';

function quiet(): HttpContext {
  return new HttpContext().set(SUPPRESS_ERROR_TOAST, true);
}

/**
 * The user's arrangement of the Agents and Projects pages: what they hid, their sort and
 * their custom order (docs/specs/agent-project-organization.md §6.1).
 *
 * **An arrangement is an enhancement.** A read that fails, or the flag being off, leaves the
 * page in today's order with nothing hidden; the page never fails because this did not load.
 *
 * Each surface is a lazy `resource()` on this root service, so going back to a page shows the
 * arrangement it already has (frontend CLAUDE.md, "Lists the user comes back to"). Nothing is
 * fetched until a page asks, and never while `features.organizeLists` is off: the backend
 * 404s `/users/me/arrangements/*` then.
 *
 * **Writes are optimistic and queued.** The page shows the change at once, and each `PUT`
 * waits for the one before it, so two quick hides reach the server in the order they were
 * made. A failed write re-reads the stored arrangement, which is what the server holds, and
 * rethrows for the page to report.
 */
@Injectable({ providedIn: 'root' })
export class ListArrangementService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly features = inject(FEATURES);

  private readonly surfaces = {
    agents: this.surfaceState('agents'),
    projects: this.surfaceState('projects'),
  } as const;

  /** Whether this build offers Hide and the arrangement controls at all. */
  get enabled(): boolean {
    return this.features.organizeLists;
  }

  /**
   * Load a page's arrangement. The first call loads; later ones refresh behind the copy
   * already shown, so a change made in another tab still arrives.
   */
  open(surface: ArrangementSurface): void {
    if (!this.enabled) return;
    const state = this.surfaces[surface];
    if (!state.wanted()) {
      state.wanted.set(true);
      return;
    }
    state.resource.reload();
  }

  /** The arrangement the page should use: the stored one, or the defaults. */
  arrangement(surface: ArrangementSurface): Signal<Arrangement> {
    return this.surfaces[surface].current;
  }

  /** The ids the user hid on `surface`, as a set for per-row reads. */
  hiddenIds(surface: ArrangementSurface): Signal<ReadonlySet<string>> {
    return this.surfaces[surface].hiddenIds;
  }

  /**
   * Hide or unhide one item. `pageIds` is every id the page lists, in the page's order: it
   * decides what can be dropped at the backend's cap, and under a custom order it is how the
   * page's new items are folded in (spec D6).
   */
  async setHidden(surface: ArrangementSurface, id: string, hide: boolean, pageIds: readonly string[]): Promise<void> {
    await this.update(surface, pageIds, (before) => ({
      ...before,
      hidden: withHidden(before.hidden, id, hide, new Set(pageIds)),
    }));
  }

  /** Choose Recently added, Name or Custom. */
  async setSort(surface: ArrangementSurface, sort: ArrangementSort, pageIds: readonly string[]): Promise<void> {
    await this.update(surface, pageIds, (before) => foldNew({ ...before, sort }, pageIds));
  }

  /** Save an Arrange → Done: `arrangedIds` top first, `hiddenIds` hidden, sort Custom. */
  async saveArranged(
    surface: ArrangementSurface,
    arrangedIds: readonly string[],
    hiddenIds: ReadonlySet<string>,
    pageIds: readonly string[],
  ): Promise<void> {
    await this.update(surface, pageIds, (before) => withArranged(before, arrangedIds, hiddenIds, pageIds));
  }

  private async update(
    surface: ArrangementSurface,
    pageIds: readonly string[],
    change: (before: Arrangement) => Arrangement,
  ): Promise<void> {
    if (!this.enabled) return;
    const state = this.surfaces[surface];
    const before = foldNew(state.current(), pageIds);
    // A save is what makes an arrangement "saved", so New badges start from here (spec D6).
    const next: Arrangement = { ...change(before), updatedAt: before.updatedAt ?? new Date().toISOString() };
    state.resource.set(next);
    await this.enqueue(state, next);
  }

  private surfaceState(surface: ArrangementSurface) {
    const wanted = signal(false);
    const arrangementResource = resource({
      params: () => (this.features.organizeLists && wanted() ? surface : undefined),
      loader: ({ params }) => this.read(params),
    });
    const current = computed<Arrangement>(() =>
      arrangementResource.hasValue() ? arrangementResource.value() : DEFAULT_ARRANGEMENT,
    );
    const hiddenIds = computed<ReadonlySet<string>>(() => new Set(current().hidden));
    return { surface, wanted, resource: arrangementResource, current, hiddenIds, queue: { tail: Promise.resolve() } };
  }

  private async read(surface: ArrangementSurface): Promise<Arrangement> {
    try {
      return await firstValueFrom(this.http.get<Arrangement>(this.url(surface), { context: quiet() }));
    } catch {
      return { ...DEFAULT_ARRANGEMENT };
    }
  }

  private enqueue(state: ReturnType<ListArrangementService['surfaceState']>, next: Arrangement): Promise<void> {
    const write = state.queue.tail.then(async () => {
      try {
        await firstValueFrom(
          this.http.put<Arrangement>(
            this.url(state.surface),
            { sort: next.sort, order: next.order, hidden: next.hidden },
            { context: quiet() },
          ),
        );
      } catch (err) {
        state.resource.reload();
        throw err;
      }
    });
    // The queue carries on after a failure; only this write's caller sees the error.
    state.queue.tail = write.catch(() => undefined);
    return write;
  }

  private url(surface: ArrangementSurface): string {
    return `${this.config.appApiUrl()}/users/me/arrangements/${surface}`;
  }
}
