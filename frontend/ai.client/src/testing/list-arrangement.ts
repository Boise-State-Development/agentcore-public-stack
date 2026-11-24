import { computed, signal } from '@angular/core';
import { vi } from 'vitest';
import {
  Arrangement,
  ArrangementSort,
  ArrangementSurface,
  DEFAULT_ARRANGEMENT,
  foldNew,
  withArranged,
} from '../app/services/list-arrangement/list-arrangement.models';

/**
 * A stand-in for `ListArrangementService` that keeps each page's arrangement in memory, for
 * page specs. It applies the same pure rules as the real service. `fail` makes the next
 * write reject without changing anything.
 */
export function listArrangementStub() {
  const arrangements = {
    agents: signal<Arrangement>({ ...DEFAULT_ARRANGEMENT }),
    projects: signal<Arrangement>({ ...DEFAULT_ARRANGEMENT }),
  };
  const hiddenIds = {
    agents: computed<ReadonlySet<string>>(() => new Set(arrangements.agents().hidden)),
    projects: computed<ReadonlySet<string>>(() => new Set(arrangements.projects().hidden)),
  };
  let failNext = false;

  function write(surface: ArrangementSurface, pageIds: readonly string[], change: (a: Arrangement) => Arrangement): void {
    if (failNext) {
      failNext = false;
      throw new Error('503');
    }
    const before = foldNew(arrangements[surface](), pageIds);
    arrangements[surface].set({ ...change(before), updatedAt: before.updatedAt ?? '2026-11-23T00:00:00Z' });
  }

  return {
    arrangements,
    /** Set a surface's hidden ids directly, as a stored arrangement would. */
    hide: (surface: ArrangementSurface, ...ids: string[]) =>
      arrangements[surface].update((a) => ({ ...a, hidden: [...a.hidden, ...ids] })),
    fail: () => {
      failNext = true;
    },
    open: vi.fn(),
    arrangement: (surface: ArrangementSurface) => arrangements[surface],
    hiddenIds: (surface: ArrangementSurface) => hiddenIds[surface],
    setHidden: vi.fn(async (surface: ArrangementSurface, id: string, hide: boolean, pageIds: readonly string[]) =>
      write(surface, pageIds, (a) => ({
        ...a,
        hidden: hide ? [...a.hidden.filter((h) => h !== id), id] : a.hidden.filter((h) => h !== id),
      })),
    ),
    setSort: vi.fn(async (surface: ArrangementSurface, sort: ArrangementSort, pageIds: readonly string[]) =>
      write(surface, pageIds, (a) => foldNew({ ...a, sort }, pageIds)),
    ),
    saveArranged: vi.fn(
      async (surface: ArrangementSurface, ids: readonly string[], hidden: ReadonlySet<string>, pageIds: readonly string[]) =>
        write(surface, pageIds, (a) => withArranged(a, ids, hidden, pageIds)),
    ),
  };
}
