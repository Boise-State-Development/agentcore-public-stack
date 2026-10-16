import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRouteSnapshot, NavigationEnd, NavigationStart, Router } from '@angular/router';

/** The shell's scroll container (`app.html`). The window itself never scrolls. */
export const APP_SCROLL_CONTAINER_ID = 'app-scroll-container';

/**
 * Route `data.scroll` value for a page that positions the shell's scroll
 * container itself. The conversation page does: it restores a remembered
 * offset per conversation or anchors the latest turn, and sticks to the
 * bottom while a reply streams (`ScrollPositionService`). Navigation scroll
 * handling leaves such a route entirely alone, in both directions.
 */
export const SELF_MANAGED_SCROLL = 'self';

/** How long a restore waits for the page's content to grow tall enough. */
export const RESTORE_TIMEOUT_MS = 2000;

/** Remembered offsets kept per tab; the oldest is dropped past this. */
export const MAX_REMEMBERED_POSITIONS = 100;

/** Input that means the user has started scrolling, so a pending restore must not yank the page. */
const USER_SCROLL_EVENTS = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;

/**
 * Whether the active route positions the scroll container itself. Deepest
 * declared wins, as with `data.chrome` (see `resolveRouteChrome`): a parent
 * can declare it for its children and a child can opt back out.
 */
export function routeManagesOwnScroll(root: ActivatedRouteSnapshot | null | undefined): boolean {
  let route = root;
  let scroll: unknown = null;
  while (route) {
    const declared = route.data?.['scroll'];
    if (typeof declared === 'string') scroll = declared;
    route = route.firstChild;
  }
  return scroll === SELF_MANAGED_SCROLL;
}

/** `/a/b?x=1#frag` → `{ key: '/a/b?x=1', path: '/a/b', fragment: 'frag' }`. */
export function splitUrl(url: string): { key: string; path: string; fragment: string | null } {
  const hash = url.indexOf('#');
  const key = hash === -1 ? url : url.slice(0, hash);
  const fragment = hash === -1 ? null : decodeURIComponent(url.slice(hash + 1)) || null;
  const query = key.indexOf('?');
  return { key, path: query === -1 ? key : key.slice(0, query), fragment };
}

/**
 * Scroll position across route changes, for the shell's scroll container.
 *
 * Angular's own restoration (`withInMemoryScrolling`) moves the window, and
 * the window never scrolls here, so without this the previous page's offset
 * carried into whatever rendered next in the same container: a page opened
 * for the first time could land half way down.
 *
 * The policy, per URL (path + query) for the life of the tab:
 * - leaving a page remembers its offset;
 * - arriving at a page restores the remembered offset (Back/Forward, or
 *   re-opening it from the nav), or starts at the top on a first visit;
 * - a navigation that only changes the query string (filters, tabs, the
 *   conversation page's own `replaceUrl` rewrites) does not scroll at all;
 * - a URL with a fragment scrolls to that element once it renders;
 * - a route declaring `data: { scroll: 'self' }` is left alone.
 *
 * Offsets are not persisted. After a reload, content heights differ enough
 * that a stored pixel offset points somewhere arbitrary (the same reasoning
 * as `ScrollPositionService`).
 */
@Injectable({ providedIn: 'root' })
export class RouteScrollService {
  private readonly router = inject(Router);
  private readonly document = inject(DOCUMENT);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private readonly positions = new Map<string, number>();

  /** The page on screen, as of the last completed navigation. */
  private current: { key: string; path: string; selfManaged: boolean } | null = null;

  /** Cancels a restore or anchor scroll still waiting for content. */
  private cancelPending: (() => void) | null = null;

  constructor() {
    if (!this.isBrowser) return;
    this.router.events.pipe(takeUntilDestroyed()).subscribe((event) => {
      if (event instanceof NavigationStart) this.onNavigationStart();
      else if (event instanceof NavigationEnd) this.onNavigationEnd(event);
    });
  }

  private get container(): HTMLElement | null {
    return this.document.getElementById(APP_SCROLL_CONTAINER_ID);
  }

  private onNavigationStart(): void {
    this.cancel();
    const container = this.container;
    if (!this.current || this.current.selfManaged || !container) return;
    this.remember(this.current.key, container.scrollTop);
  }

  private onNavigationEnd(event: NavigationEnd): void {
    const { key, path, fragment } = splitUrl(event.urlAfterRedirects);
    const previousPath = this.current?.path ?? null;
    const selfManaged = routeManagesOwnScroll(this.router.routerState.snapshot.root);
    this.current = { key, path, selfManaged };

    const container = this.container;
    if (selfManaged || !container) return;
    if (path === previousPath && !fragment) return;

    container.scrollTop = 0;
    if (fragment) {
      this.whenReady(container, () => {
        const target = this.document.getElementById(fragment);
        target?.scrollIntoView({ block: 'start' });
        return !!target;
      });
      return;
    }

    const saved = this.positions.get(key);
    if (!saved) return;
    this.whenReady(
      container,
      () => {
        if (container.scrollHeight - container.clientHeight < saved) return false;
        container.scrollTop = saved;
        return true;
      },
      // Content never grew tall enough (a list got shorter): get as close as it allows.
      () => (container.scrollTop = saved),
    );
  }

  private remember(key: string, offset: number): void {
    this.positions.delete(key);
    this.positions.set(key, offset);
    if (this.positions.size > MAX_REMEMBERED_POSITIONS) {
      this.positions.delete(this.positions.keys().next().value!);
    }
  }

  /**
   * Run `attempt` each animation frame until it reports success, the user
   * starts scrolling, the next navigation begins, or the timeout passes
   * (then `onTimeout`). The first try waits a frame so the new page has
   * rendered; a page whose data loads asynchronously gets the rest.
   */
  private whenReady(container: HTMLElement, attempt: () => boolean, onTimeout?: () => void): void {
    const deadline = performance.now() + RESTORE_TIMEOUT_MS;
    let frame = 0;
    const stop = () => {
      cancelAnimationFrame(frame);
      for (const type of USER_SCROLL_EVENTS) container.removeEventListener(type, stop);
      this.document.removeEventListener('keydown', stop);
      if (this.cancelPending === stop) this.cancelPending = null;
    };
    const tick = () => {
      if (attempt()) return stop();
      if (performance.now() >= deadline) {
        onTimeout?.();
        return stop();
      }
      frame = requestAnimationFrame(tick);
    };

    for (const type of USER_SCROLL_EVENTS) container.addEventListener(type, stop, { passive: true });
    this.document.addEventListener('keydown', stop, { passive: true });
    this.cancelPending = stop;
    frame = requestAnimationFrame(tick);
  }

  private cancel(): void {
    this.cancelPending?.();
  }
}
