/** sessionStorage key holding the epoch-ms of the last automatic reload. */
export const AUTO_RELOAD_STORAGE_KEY = 'app-update:auto-reload-at';

/**
 * At most one automatic reload per tab in this window.
 *
 * A reload loop (the fresh shell *also* names a chunk that will not load — a
 * CDN outage, a half-finished sync) repeats within seconds, so any window of
 * a minute or more stops it on the second pass. Five minutes leaves room for a
 * slow reload on a bad connection, and stays short enough that a second deploy
 * later in the same session still recovers silently rather than prompting.
 */
export const AUTO_RELOAD_WINDOW_MS = 5 * 60 * 1000;

/**
 * Claim this tab's automatic reload, if it has not used one recently.
 *
 * Returns `true` — and records the claim — when a reload is allowed; `false`
 * when one already happened inside the window. sessionStorage because it is
 * per tab and survives exactly the reload being guarded.
 *
 * **Fails closed.** Without working storage there is no way to know whether
 * the last page load was already this reload, so an unreadable or unwritable
 * store answers `false` and the caller prompts instead of risking a loop.
 */
export function claimAutoReload(
  storage: Storage | null,
  now: number,
  windowMs: number = AUTO_RELOAD_WINDOW_MS,
): boolean {
  if (!storage) return false;
  try {
    const last = Number(storage.getItem(AUTO_RELOAD_STORAGE_KEY));
    // A clock that moved backwards (last > now) reads as "just reloaded".
    if (Number.isFinite(last) && last > 0 && now - last < windowMs) return false;
    storage.setItem(AUTO_RELOAD_STORAGE_KEY, String(now));
    return storage.getItem(AUTO_RELOAD_STORAGE_KEY) === String(now);
  } catch {
    return false;
  }
}

/** The tab's sessionStorage, or `null` where the browser refuses it. */
export function sessionStorageOrNull(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}
