// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRouteSnapshot, Router, provideRouter } from '@angular/router';
import {
  APP_SCROLL_CONTAINER_ID,
  RESTORE_TIMEOUT_MS,
  RouteScrollService,
  routeManagesOwnScroll,
  splitUrl,
} from './route-scroll.service';

@Component({ selector: 'app-stub-page', template: '' })
class StubPage {}

describe('RouteScrollService', () => {
  let container: HTMLDivElement;
  let router: Router;
  let frames: Map<number, FrameRequestCallback>;
  let nextFrame: number;
  let now: number;
  /** What the container could scroll to if asked: scrollHeight - clientHeight. */
  let scrollRoom: number;

  /** Run queued animation frames, `count` rounds deep. */
  function flushFrames(count = 1): void {
    for (let i = 0; i < count; i++) {
      const queued = [...frames.values()];
      frames.clear();
      for (const cb of queued) cb(now);
    }
  }

  async function go(url: string): Promise<void> {
    await router.navigateByUrl(url);
  }

  beforeEach(() => {
    frames = new Map();
    nextFrame = 0;
    now = 0;
    scrollRoom = 5000;
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      frames.set(++nextFrame, cb);
      return nextFrame;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
    vi.spyOn(performance, 'now').mockImplementation(() => now);

    container = document.createElement('div');
    container.id = APP_SCROLL_CONTAINER_ID;
    Object.defineProperty(container, 'clientHeight', { configurable: true, get: () => 800 });
    Object.defineProperty(container, 'scrollHeight', { configurable: true, get: () => scrollRoom + 800 });
    document.body.appendChild(container);

    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'tools', component: StubPage },
          { path: 'models', component: StubPage },
          { path: 'chat', component: StubPage, data: { scroll: 'self' } },
          { path: 'docs', component: StubPage },
        ]),
      ],
    });
    TestBed.inject(RouteScrollService);
    router = TestBed.inject(Router);
  });

  afterEach(() => {
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('starts a page opened for the first time at the top', async () => {
    await go('/tools');
    container.scrollTop = 1200;

    await go('/models');

    expect(container.scrollTop).toBe(0);
  });

  it('restores the offset a page had when the user returns to it', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');
    container.scrollTop = 300;

    await go('/tools');
    flushFrames();

    expect(container.scrollTop).toBe(1200);
  });

  it('remembers each page separately', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');
    container.scrollTop = 300;
    await go('/tools');
    flushFrames();

    await go('/models');
    flushFrames();

    expect(container.scrollTop).toBe(300);
  });

  it('waits for content that loads after the route renders', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');

    scrollRoom = 0;
    await go('/tools');
    flushFrames(3);
    expect(container.scrollTop).toBe(0);

    scrollRoom = 5000;
    flushFrames();
    expect(container.scrollTop).toBe(1200);
  });

  it('gets as close as the content allows when it never grows tall enough', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');

    scrollRoom = 400;
    await go('/tools');
    flushFrames(2);
    expect(container.scrollTop).toBe(0);

    now = RESTORE_TIMEOUT_MS;
    flushFrames();
    // jsdom does not clamp; a browser would clamp this to the 400px available.
    expect(container.scrollTop).toBe(1200);
    expect(frames.size).toBe(0);
  });

  it('abandons a pending restore once the user starts scrolling', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');

    scrollRoom = 0;
    await go('/tools');
    container.dispatchEvent(new Event('wheel'));
    scrollRoom = 5000;
    flushFrames(3);

    expect(container.scrollTop).toBe(0);
  });

  it('does not scroll when only the query string changes', async () => {
    await go('/tools');
    container.scrollTop = 1200;

    await go('/tools?filter=mcp');
    flushFrames();

    expect(container.scrollTop).toBe(1200);
  });

  it('leaves a route that manages its own scroll alone, arriving and leaving', async () => {
    await go('/tools');
    container.scrollTop = 1200;

    await go('/chat');
    flushFrames();
    expect(container.scrollTop).toBe(1200);

    // The chat page scrolled itself; leaving it must not record that offset
    // against anything, and the next page still starts at the top.
    container.scrollTop = 9000;
    await go('/models');
    expect(container.scrollTop).toBe(0);
  });

  it('cancels a pending restore when the next navigation starts', async () => {
    await go('/tools');
    container.scrollTop = 1200;
    await go('/models');

    scrollRoom = 0;
    await go('/tools');
    await go('/chat');
    container.scrollTop = 50;
    scrollRoom = 5000;
    flushFrames(3);

    expect(container.scrollTop).toBe(50);
  });

  it('scrolls to the fragment target instead of restoring', async () => {
    const anchor = document.createElement('section');
    anchor.id = 'limits';
    anchor.scrollIntoView = vi.fn();
    document.body.appendChild(anchor);

    await go('/tools');
    container.scrollTop = 1200;
    await go('/docs#limits');
    flushFrames();

    expect(container.scrollTop).toBe(0);
    expect(anchor.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
    anchor.remove();
  });
});

describe('routeManagesOwnScroll', () => {
  function node(data: Record<string, unknown>, firstChild: unknown = null): ActivatedRouteSnapshot {
    return { data, firstChild } as unknown as ActivatedRouteSnapshot;
  }

  it('reads the flag off the deepest route that declares one', () => {
    expect(routeManagesOwnScroll(node({}, node({ scroll: 'self' })))).toBe(true);
    expect(routeManagesOwnScroll(node({ scroll: 'self' }, node({})))).toBe(true);
    expect(routeManagesOwnScroll(node({ scroll: 'self' }, node({ scroll: 'router' })))).toBe(false);
    expect(routeManagesOwnScroll(node({}, node({})))).toBe(false);
    expect(routeManagesOwnScroll(null)).toBe(false);
  });
});

describe('splitUrl', () => {
  it('keys on path and query, and pulls the fragment out', () => {
    expect(splitUrl('/a/b?x=1#frag')).toEqual({ key: '/a/b?x=1', path: '/a/b', fragment: 'frag' });
    expect(splitUrl('/a/b')).toEqual({ key: '/a/b', path: '/a/b', fragment: null });
    expect(splitUrl('/a#')).toEqual({ key: '/a', path: '/a', fragment: null });
  });
});
