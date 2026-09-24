import { DOCUMENT, Location } from '@angular/common';
import { DestroyRef, Injectable, InjectionToken, inject } from '@angular/core';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { FileUploadService } from '../file-upload/file-upload.service';
import { ToastService } from '../toast/toast.service';
import { claimAutoReload, sessionStorageOrNull } from './reload-guard';

/** Full page loads, behind a token so specs can observe them instead of unloading the runner. */
export interface PageLoader {
  /** Load `href` as a fresh document. */
  assign(href: string): void;
  /** Reload the current document. */
  reload(): void;
}

export const PAGE_LOADER = new InjectionToken<PageLoader>('PAGE_LOADER', {
  providedIn: 'root',
  factory: () => {
    const location = inject(DOCUMENT).location;
    return {
      assign: href => location.assign(href),
      reload: () => location.reload(),
    };
  },
});

/** Where the reload guard keeps its timestamp; a token so specs can supply a fake or a broken store. */
export const RELOAD_GUARD_STORAGE = new InjectionToken<() => Storage | null>(
  'RELOAD_GUARD_STORAGE',
  { providedIn: 'root', factory: () => sessionStorageOrNull },
);

/**
 * Least time between two proactive version checks.
 *
 * A check is one conditional GET of `index.html` — a 304 with no body when
 * nothing changed, since the shell is served `no-cache`. It runs only when the
 * tab becomes visible again, which is when a stale tab is found: someone
 * coming back to a tab they left open across a deploy. Ten minutes keeps a
 * user flicking between windows from issuing a request per switch, and is far
 * shorter than the gap between deploys, so a returning user still hears about
 * a new build on their first look.
 */
export const VERSION_CHECK_MIN_INTERVAL_MS = 10 * 60 * 1000;

/**
 * The content hash of a `main-XXXXXXXX.js` bundle referenced in `text`, or
 * `null` when there is none (the dev server's `main.js` is unhashed, which
 * switches the proactive check off locally).
 */
export function mainBundleHash(text: string): string | null {
  return /\bmain-([A-Z0-9]{8})\.js\b/.exec(text)?.[1] ?? null;
}

const PROMPT_TITLE = 'A new version of the app is available';
const PROMPT_MESSAGE = 'Refresh to load the latest version.';
const PROMPT_MESSAGE_BUSY =
  'Refresh once the response or upload in progress finishes — refreshing now would interrupt it.';

/**
 * Keeps a tab that outlived a deploy working.
 *
 * `deploy.sh` removes the previous build's hashed chunks, so a tab opened
 * before a deploy fails the first time it navigates to a lazy route it has not
 * loaded yet (see `isChunkLoadError`). The fix for the user is a full page
 * load, which fetches the new shell and its new chunk names; this service does
 * that for them when it is safe, and asks them when it is not.
 *
 * - **Failed navigation** → full load of the URL they were going to, unless a
 *   chat response is streaming or a file is uploading (both survive in-app
 *   navigation but not a page load), or the tab already auto-reloaded within
 *   the guard window (the reload did not fix it, so a loop would follow).
 *   Either way it falls back to the refresh prompt.
 * - **Any other chunk failure** (a `@defer` block, a lazily imported library)
 *   → the prompt only. The user did not ask to leave the page they are on.
 * - **Proactive check** → the prompt, when the tab comes back into view and
 *   the served shell names a different `main` bundle from the one running.
 *
 * Composer drafts are safe across any of these loads: the composer mirrors
 * itself to `localStorage` on every change (`ComposerDraftStorageService`),
 * so there is nothing to flush before unloading.
 */
@Injectable({ providedIn: 'root' })
export class AppUpdateService {
  private readonly document = inject(DOCUMENT);
  private readonly location = inject(Location);
  private readonly destroyRef = inject(DestroyRef);
  private readonly toast = inject(ToastService);
  private readonly chatState = inject(ChatStateService);
  private readonly fileUploads = inject(FileUploadService);
  private readonly pageLoader = inject(PAGE_LOADER);
  private readonly guardStorage = inject(RELOAD_GUARD_STORAGE);

  /** Whether this tab is known to be running an older build than the one deployed. */
  private staleBuildDetected = false;

  /** Set once a page load is under way; later failures from the unloading page are noise. */
  private reloading = false;

  /** The live refresh prompt, so a burst of failures shows one toast, not several. */
  private promptToastId: string | null = null;

  /** Where the prompt's Refresh goes; `null` reloads the current page. */
  private refreshHref: string | null = null;

  private lastVersionCheckAt = 0;
  private versionCheckInFlight = false;

  /**
   * A navigation to `targetUrl` (a router URL such as `/settings/api-keys`)
   * failed to load its chunk.
   *
   * The browser's address bar still shows the page being left — the router
   * defers the URL update until a navigation succeeds — so the reload targets
   * the destination explicitly rather than calling `location.reload()`.
   */
  recoverFromFailedNavigation(targetUrl: string): void {
    if (this.reloading) return;
    this.staleBuildDetected = true;
    const href = this.location.prepareExternalUrl(targetUrl);

    if (!this.hasWorkInFlight() && claimAutoReload(this.guardStorage(), Date.now())) {
      this.reloading = true;
      this.pageLoader.assign(href);
      return;
    }
    this.promptRefresh(href);
  }

  /** A chunk failed to load outside a navigation. Prompt; never reload out from under the user. */
  recoverFromChunkLoadError(): void {
    if (this.reloading) return;
    this.staleBuildDetected = true;
    this.promptRefresh(null);
  }

  /**
   * Watch for a newer deploy while this tab is open. Does nothing when the
   * running shell has no hashed `main` bundle (the dev server).
   */
  startVersionCheck(): void {
    const running = this.runningMainBundleHash();
    if (!running) return;

    const onVisibilityChange = (): void => {
      if (this.document.visibilityState === 'visible') void this.checkForNewBuild(running);
    };
    this.document.addEventListener('visibilitychange', onVisibilityChange);
    this.destroyRef.onDestroy(() =>
      this.document.removeEventListener('visibilitychange', onVisibilityChange),
    );
  }

  /**
   * Compare the `main` bundle this tab is running against the one the
   * currently served `index.html` names, and prompt if they differ.
   * Throttled; every failure is silent — this is an optimisation over the
   * navigation recovery, which still catches whatever this misses.
   */
  async checkForNewBuild(running: string, now: number = Date.now()): Promise<void> {
    if (this.staleBuildDetected || this.versionCheckInFlight) return;
    if (now - this.lastVersionCheckAt < VERSION_CHECK_MIN_INTERVAL_MS) return;
    this.lastVersionCheckAt = now;
    this.versionCheckInFlight = true;
    try {
      const shellUrl = new URL('index.html', this.document.baseURI).href;
      const response = await fetch(shellUrl, { cache: 'no-cache' });
      if (!response.ok) return;
      const served = mainBundleHash(await response.text());
      if (served && served !== running) {
        this.staleBuildDetected = true;
        this.promptRefresh(null);
      }
    } catch {
      // Offline, or the shell is unreachable — try again on a later focus.
    } finally {
      this.versionCheckInFlight = false;
    }
  }

  private promptRefresh(href: string | null): void {
    // A navigation's destination beats "reload where I am" — it is where the
    // user was trying to go.
    if (href) this.refreshHref = href;
    const live =
      this.promptToastId !== null &&
      this.toast.toasts().some(toast => toast.id === this.promptToastId);
    if (live) return;

    this.promptToastId = this.toast.info(
      PROMPT_TITLE,
      this.hasWorkInFlight() ? PROMPT_MESSAGE_BUSY : PROMPT_MESSAGE,
      {
        duration: 0,
        action: { label: 'Refresh', handler: () => this.refresh() },
      },
    );
  }

  private refresh(): void {
    this.reloading = true;
    if (this.refreshHref) {
      this.pageLoader.assign(this.refreshHref);
    } else {
      this.pageLoader.reload();
    }
  }

  /** Work a page load would cut off: a streaming response or an upload in progress. */
  private hasWorkInFlight(): boolean {
    return this.chatState.anySessionLoading() || this.fileUploads.hasActivePendingUploads();
  }

  private runningMainBundleHash(): string | null {
    const scripts = Array.from(this.document.querySelectorAll('script[src]'));
    for (const script of scripts) {
      const hash = mainBundleHash(script.getAttribute('src') ?? '');
      if (hash) return hash;
    }
    return null;
  }
}
