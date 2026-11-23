import { signal } from '@angular/core';
import { vi } from 'vitest';
import type { ArrangementSurface } from '../app/services/list-arrangement/list-arrangement.models';

/**
 * A stand-in for `ListArrangementService` that keeps hidden ids in memory, for page specs.
 * `fail` makes the next `setHidden` reject without changing anything.
 */
export function listArrangementStub() {
  const hidden = {
    agents: signal<ReadonlySet<string>>(new Set()),
    projects: signal<ReadonlySet<string>>(new Set()),
  };
  let failNext = false;
  return {
    hidden,
    fail: () => {
      failNext = true;
    },
    open: vi.fn(),
    hiddenIds: (surface: ArrangementSurface) => hidden[surface],
    setHidden: vi.fn(async (surface: ArrangementSurface, id: string, hide: boolean) => {
      if (failNext) {
        failNext = false;
        throw new Error('503');
      }
      const next = new Set(hidden[surface]());
      if (hide) next.add(id);
      else next.delete(id);
      hidden[surface].set(next);
    }),
  };
}
