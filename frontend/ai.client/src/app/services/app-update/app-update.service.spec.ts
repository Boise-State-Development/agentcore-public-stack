import { APP_BASE_HREF } from '@angular/common';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { NavigationError, provideRouter } from '@angular/router';
import { Mock, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { FileUploadService } from '../file-upload/file-upload.service';
import { ToastService } from '../toast/toast.service';
import { ChunkLoadErrorHandler, recoverFromStaleChunkNavigation } from './app-update.providers';
import {
  AppUpdateService,
  PAGE_LOADER,
  PageLoader,
  RELOAD_GUARD_STORAGE,
  VERSION_CHECK_MIN_INTERVAL_MS,
  mainBundleHash,
} from './app-update.service';
import { AUTO_RELOAD_STORAGE_KEY } from './reload-guard';

const CHUNK_ERROR = new TypeError(
  'Failed to fetch dynamically imported module: https://boisestate.ai/chunk-4DOJ3VBG.js',
);
const PROMPT_TITLE = 'A new version of the app is available';

/** Minimal in-memory Storage — the real sessionStorage is shared across spec files. */
function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: key => items.get(key) ?? null,
    key: index => [...items.keys()][index] ?? null,
    removeItem: key => void items.delete(key),
    setItem: (key, value) => void items.set(key, value),
  };
}

function shellHtml(hash: string): string {
  return `<!doctype html><html><head><base href="/"></head><body><app-root></app-root>
<script src="polyfills-ABCD1234.js" type="module"></script>
<script src="main-${hash}.js" type="module"></script></body></html>`;
}

describe('AppUpdateService', () => {
  let service: AppUpdateService;
  let toast: ToastService;
  let chatState: ChatStateService;
  let pageLoader: { assign: Mock<(href: string) => void>; reload: Mock<() => void> };
  let storage: Storage | null;
  let uploadsActive: ReturnType<typeof signal<boolean>>;

  beforeEach(() => {
    pageLoader = { assign: vi.fn<(href: string) => void>(), reload: vi.fn<() => void>() };
    storage = memoryStorage();
    uploadsActive = signal(false);

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: APP_BASE_HREF, useValue: '/app/' },
        { provide: PAGE_LOADER, useValue: pageLoader satisfies PageLoader },
        { provide: RELOAD_GUARD_STORAGE, useValue: () => storage },
        { provide: FileUploadService, useValue: { hasActivePendingUploads: uploadsActive } },
      ],
    });
    service = TestBed.inject(AppUpdateService);
    toast = TestBed.inject(ToastService);
    chatState = TestBed.inject(ChatStateService);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  const prompts = () => toast.toasts().filter(t => t.title === PROMPT_TITLE);

  describe('failed navigation', () => {
    it('does a full load of the destination, under the base href', () => {
      service.recoverFromFailedNavigation('/settings/api-keys');

      expect(pageLoader.assign).toHaveBeenCalledExactlyOnceWith('/app/settings/api-keys');
      expect(pageLoader.reload).not.toHaveBeenCalled();
      expect(prompts()).toHaveLength(0);
      expect(storage?.getItem(AUTO_RELOAD_STORAGE_KEY)).not.toBeNull();
    });

    it('prompts instead of reloading again when the last auto-reload was recent', () => {
      // The page that just auto-reloaded failed again: reloading would loop.
      storage?.setItem(AUTO_RELOAD_STORAGE_KEY, String(Date.now() - 3_000));

      service.recoverFromFailedNavigation('/settings/api-keys');

      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(prompts()).toHaveLength(1);
    });

    it('prompts when the guard cannot record anything (storage blocked)', () => {
      storage = null;
      service.recoverFromFailedNavigation('/settings/api-keys');
      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(prompts()).toHaveLength(1);
    });

    it('does not cut off a response streaming in another conversation', () => {
      chatState.setChatLoading('session-a', true);
      chatState.setViewedSession('session-b');

      service.recoverFromFailedNavigation('/settings/api-keys');

      expect(pageLoader.assign).not.toHaveBeenCalled();
      const [prompt] = prompts();
      expect(prompt.message).toMatch(/interrupt/);
      // Nothing was spent: the auto-reload is still available once it's safe.
      expect(storage?.getItem(AUTO_RELOAD_STORAGE_KEY)).toBeNull();
    });

    it('does not cut off an upload in progress', () => {
      uploadsActive.set(true);
      service.recoverFromFailedNavigation('/settings/api-keys');
      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(prompts()).toHaveLength(1);
    });

    it("sends the prompt's Refresh to the destination, not the page being left", () => {
      uploadsActive.set(true);
      service.recoverFromFailedNavigation('/settings/api-keys');

      prompts()[0].action?.handler();

      expect(pageLoader.assign).toHaveBeenCalledExactlyOnceWith('/app/settings/api-keys');
    });

    it('ignores further failures once a reload is under way', () => {
      service.recoverFromFailedNavigation('/settings/api-keys');
      service.recoverFromFailedNavigation('/settings/profile');
      service.recoverFromChunkLoadError();

      expect(pageLoader.assign).toHaveBeenCalledTimes(1);
      expect(prompts()).toHaveLength(0);
    });
  });

  describe('chunk failure outside a navigation', () => {
    it('prompts and never reloads on its own', () => {
      service.recoverFromChunkLoadError();

      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(pageLoader.reload).not.toHaveBeenCalled();
      const [prompt] = prompts();
      expect(prompt.duration).toBe(0);
      expect(prompt.action?.label).toBe('Refresh');
    });

    it('shows one prompt for a burst of failures', () => {
      service.recoverFromChunkLoadError();
      service.recoverFromChunkLoadError();
      service.recoverFromChunkLoadError();
      expect(prompts()).toHaveLength(1);
    });

    it('prompts again after the user dismissed the first one', () => {
      service.recoverFromChunkLoadError();
      toast.dismiss(prompts()[0].id);
      service.recoverFromChunkLoadError();
      expect(prompts()).toHaveLength(1);
    });

    it('reloads the current page from the Refresh action', () => {
      service.recoverFromChunkLoadError();
      prompts()[0].action?.handler();
      expect(pageLoader.reload).toHaveBeenCalledTimes(1);
    });
  });

  describe('router and ErrorHandler wiring', () => {
    it('routes a chunk-load NavigationError to a full load', () => {
      TestBed.runInInjectionContext(() =>
        recoverFromStaleChunkNavigation(new NavigationError(1, '/settings/api-keys', CHUNK_ERROR)),
      );
      expect(pageLoader.assign).toHaveBeenCalledExactlyOnceWith('/app/settings/api-keys');
    });

    it('leaves every other NavigationError alone', () => {
      TestBed.runInInjectionContext(() =>
        recoverFromStaleChunkNavigation(
          new NavigationError(1, '/settings/api-keys', new Error('Guard exploded')),
        ),
      );
      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(prompts()).toHaveLength(0);
    });

    it('ErrorHandler prompts on a chunk failure and still logs it', () => {
      const log = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      const handler = TestBed.runInInjectionContext(() => new ChunkLoadErrorHandler());

      handler.handleError({ rejection: CHUNK_ERROR });

      expect(prompts()).toHaveLength(1);
      expect(pageLoader.assign).not.toHaveBeenCalled();
      expect(log).toHaveBeenCalled();
    });

    it('ErrorHandler passes other errors straight through', () => {
      const log = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      const handler = TestBed.runInInjectionContext(() => new ChunkLoadErrorHandler());

      handler.handleError(new TypeError('Cannot read properties of undefined'));

      expect(prompts()).toHaveLength(0);
      expect(log).toHaveBeenCalled();
    });
  });

  describe('proactive version check', () => {
    const T0 = 1_800_000_000_000;

    function stubShell(hash: string): ReturnType<typeof vi.fn> {
      const fetchMock = vi.fn(async () => new Response(shellHtml(hash), { status: 200 }));
      vi.stubGlobal('fetch', fetchMock);
      return fetchMock;
    }

    it('prompts when the served shell names a different main bundle', async () => {
      const fetchMock = stubShell('NEWBUILD');
      await service.checkForNewBuild('OLDBUILD', T0);

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toMatch(/\/index\.html$/);
      expect(init.cache).toBe('no-cache');
      expect(prompts()).toHaveLength(1);
      expect(pageLoader.reload).not.toHaveBeenCalled();
    });

    it('stays quiet when the build is current', async () => {
      stubShell('SAMEBILD');
      await service.checkForNewBuild('SAMEBILD', T0);
      expect(prompts()).toHaveLength(0);
    });

    it(`checks at most once per ${VERSION_CHECK_MIN_INTERVAL_MS / 60_000} minutes`, async () => {
      const fetchMock = stubShell('SAMEBILD');
      await service.checkForNewBuild('SAMEBILD', T0);
      await service.checkForNewBuild('SAMEBILD', T0 + VERSION_CHECK_MIN_INTERVAL_MS - 1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await service.checkForNewBuild('SAMEBILD', T0 + VERSION_CHECK_MIN_INTERVAL_MS);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('stops checking once a newer build is known', async () => {
      const fetchMock = stubShell('NEWBUILD');
      await service.checkForNewBuild('OLDBUILD', T0);
      await service.checkForNewBuild('OLDBUILD', T0 + VERSION_CHECK_MIN_INTERVAL_MS * 5);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('swallows a network failure', async () => {
      vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
      await expect(service.checkForNewBuild('OLDBUILD', T0)).resolves.toBeUndefined();
      expect(prompts()).toHaveLength(0);
    });

    it('ignores a non-OK response and a shell with no hashed main bundle', async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('Forbidden', { status: 403 })));
      await service.checkForNewBuild('OLDBUILD', T0);
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response('<script src="main.js"></script>', { status: 200 })),
      );
      await service.checkForNewBuild('OLDBUILD', T0 + VERSION_CHECK_MIN_INTERVAL_MS);
      expect(prompts()).toHaveLength(0);
    });
  });
});

describe('mainBundleHash', () => {
  it('reads the hash off a production script src', () => {
    expect(mainBundleHash('main-ZMUT4FS2.js')).toBe('ZMUT4FS2');
    expect(mainBundleHash('https://boisestate.ai/main-ZMUT4FS2.js')).toBe('ZMUT4FS2');
  });

  it('finds main among the other bundles in a shell', () => {
    expect(mainBundleHash(shellHtml('ABCDEFGH'))).toBe('ABCDEFGH');
  });

  it('returns null for the unhashed dev-server bundle and for lazy chunks', () => {
    expect(mainBundleHash('main.js')).toBeNull();
    expect(mainBundleHash('chunk-4DOJ3VBG.js')).toBeNull();
  });
});
