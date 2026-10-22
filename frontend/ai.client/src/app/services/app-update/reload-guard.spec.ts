import { describe, expect, it } from 'vitest';
import { AUTO_RELOAD_STORAGE_KEY, AUTO_RELOAD_WINDOW_MS, claimAutoReload } from './reload-guard';

/** An in-memory Storage, so nothing here touches the shared jsdom sessionStorage. */
class MemoryStorage implements Storage {
  private readonly items = new Map<string, string>();
  get length(): number {
    return this.items.size;
  }
  clear(): void {
    this.items.clear();
  }
  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.items.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.items.delete(key);
  }
  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }
}

/** A store that throws on every access — a sandboxed iframe or blocked site data. */
class ThrowingStorage extends MemoryStorage {
  override getItem(): string | null {
    throw new DOMException('The operation is insecure.', 'SecurityError');
  }
}

/** A store that accepts writes and silently drops them. */
class DroppingStorage extends MemoryStorage {
  override setItem(): void {
    // Quota-exceeded browsers that swallow the write.
  }
}

const T0 = 1_800_000_000_000;

describe('claimAutoReload', () => {
  it('allows the first reload and records when it happened', () => {
    const storage = new MemoryStorage();
    expect(claimAutoReload(storage, T0)).toBe(true);
    expect(storage.getItem(AUTO_RELOAD_STORAGE_KEY)).toBe(String(T0));
  });

  it('refuses a second reload inside the window — the loop case', () => {
    const storage = new MemoryStorage();
    claimAutoReload(storage, T0);
    expect(claimAutoReload(storage, T0 + 2_000)).toBe(false);
    expect(claimAutoReload(storage, T0 + AUTO_RELOAD_WINDOW_MS - 1)).toBe(false);
  });

  it('does not extend the window when it refuses', () => {
    const storage = new MemoryStorage();
    claimAutoReload(storage, T0);
    claimAutoReload(storage, T0 + 60_000);
    expect(storage.getItem(AUTO_RELOAD_STORAGE_KEY)).toBe(String(T0));
  });

  it('allows another reload once the window has passed — a later deploy', () => {
    const storage = new MemoryStorage();
    claimAutoReload(storage, T0);
    expect(claimAutoReload(storage, T0 + AUTO_RELOAD_WINDOW_MS)).toBe(true);
    expect(storage.getItem(AUTO_RELOAD_STORAGE_KEY)).toBe(String(T0 + AUTO_RELOAD_WINDOW_MS));
  });

  it('honours a custom window', () => {
    const storage = new MemoryStorage();
    claimAutoReload(storage, T0, 1_000);
    expect(claimAutoReload(storage, T0 + 999, 1_000)).toBe(false);
    expect(claimAutoReload(storage, T0 + 1_000, 1_000)).toBe(true);
  });

  it('treats a clock that went backwards as a recent reload', () => {
    const storage = new MemoryStorage();
    claimAutoReload(storage, T0);
    expect(claimAutoReload(storage, T0 - 60_000)).toBe(false);
  });

  it('ignores a garbage timestamp', () => {
    const storage = new MemoryStorage();
    storage.setItem(AUTO_RELOAD_STORAGE_KEY, 'not-a-number');
    expect(claimAutoReload(storage, T0)).toBe(true);
  });

  describe('fails closed', () => {
    it('without storage', () => {
      expect(claimAutoReload(null, T0)).toBe(false);
    });

    it('when storage throws', () => {
      expect(claimAutoReload(new ThrowingStorage(), T0)).toBe(false);
    });

    it('when a write does not stick, so the next load could not see it', () => {
      expect(claimAutoReload(new DroppingStorage(), T0)).toBe(false);
    });
  });
});
