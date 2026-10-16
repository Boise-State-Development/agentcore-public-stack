// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Component, ElementRef, OnDestroy, inject } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Location } from '@angular/common';
import { provideLocationMocks } from '@angular/common/testing';
import { NavigationEnd, Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { filter, firstValueFrom } from 'rxjs';
import { APP_SCROLL_CONTAINER_ID } from '../../../services/route-scroll/route-scroll.service';
import { ScrollPositionService } from './scroll-position.service';

describe('ScrollPositionService', () => {
  let service: ScrollPositionService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ScrollPositionService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('returns undefined for a conversation with no remembered position', () => {
    expect(service.get('unknown')).toBeUndefined();
  });

  it('remembers positions per conversation independently', () => {
    service.save('a', 1200);
    service.save('b', 0);

    expect(service.get('a')).toBe(1200);
    expect(service.get('b')).toBe(0);
  });

  it('overwrites a conversation position on re-save', () => {
    service.save('a', 100);
    service.save('a', 900);
    expect(service.get('a')).toBe(900);
  });

  it('clears a single conversation without touching others', () => {
    service.save('a', 100);
    service.save('b', 200);

    service.clear('a');

    expect(service.get('a')).toBeUndefined();
    expect(service.get('b')).toBe(200);
  });
});

/** What the conversation page's own ngOnDestroy could see of the scroll container. */
let seenAtDestroy: { connected: boolean; scrollTop: number } | null = null;

@Component({ selector: 'app-conversation-probe', template: '<p>messages</p>' })
class ConversationProbe implements OnDestroy {
  private readonly host = inject(ElementRef<HTMLElement>);

  ngOnDestroy(): void {
    seenAtDestroy = {
      connected: this.host.nativeElement.isConnected,
      scrollTop: document.getElementById(APP_SCROLL_CONTAINER_ID)?.scrollTop ?? -1,
    };
  }
}

@Component({ selector: 'app-other-page', template: '' })
class OtherPage {}

describe('ScrollPositionService navigation capture', () => {
  let service: ScrollPositionService;
  let harness: RouterTestingHarness;
  let container: HTMLDivElement;
  /** Where the user has scrolled the conversation to. */
  let offset: number;

  /**
   * The browser clamps a scroll container's offset as soon as its content is
   * gone: with the conversation's DOM detached there is nothing to scroll, so
   * scrollTop reads 0. jsdom has no layout, so model exactly that.
   */
  function conversationOnScreen(): boolean {
    return document.querySelector('app-conversation-probe') !== null;
  }

  beforeEach(async () => {
    seenAtDestroy = null;
    offset = 0;
    container = document.createElement('div');
    container.id = APP_SCROLL_CONTAINER_ID;
    Object.defineProperty(container, 'scrollTop', {
      configurable: true,
      get: () => (conversationOnScreen() ? offset : 0),
      set: (value: number) => (offset = value),
    });
    document.body.appendChild(container);

    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 's/:sessionId', component: ConversationProbe },
          { path: 'customize/tools', component: OtherPage },
        ]),
        provideLocationMocks(),
      ],
    });
    service = TestBed.inject(ScrollPositionService);
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    container.remove();
  });

  /** Back and Forward navigate on a later task (the router defers popstate). */
  function nextNavigationEnd(): Promise<unknown> {
    return firstValueFrom(TestBed.inject(Router).events.pipe(filter((e) => e instanceof NavigationEnd)));
  }

  it('remembers the offset as the navigation away starts, while the conversation is on screen', async () => {
    await harness.navigateByUrl('/s/a');
    service.track('a');
    offset = 200;

    await harness.navigateByUrl('/customize/tools');

    expect(service.get('a')).toBe(200);
  });

  it('pins why: the router removes a page from the DOM before its ngOnDestroy runs', async () => {
    await harness.navigateByUrl('/s/a');
    offset = 200;

    await harness.navigateByUrl('/customize/tools');

    // A save from ngOnDestroy read this, and the conversation came back at the top.
    expect(seenAtDestroy).toEqual({ connected: false, scrollTop: 0 });
  });

  it('remembers the conversation being left when switching to another', async () => {
    await harness.navigateByUrl('/s/a');
    service.track('a');
    offset = 150;

    await harness.navigateByUrl('/s/b');

    expect(service.get('a')).toBe(150);
    expect(service.get('b')).toBeUndefined();
  });

  it('remembers on Back and Forward too', async () => {
    const location = TestBed.inject(Location);
    // Bootstrap installs this through initialNavigation; TestBed does not.
    TestBed.inject(Router).setUpLocationChangeListener();
    await harness.navigateByUrl('/s/a');
    service.track('a');
    offset = 200;
    await harness.navigateByUrl('/customize/tools');
    service.untrack();

    const backDone = nextNavigationEnd();
    location.back();
    await backDone;
    expect(harness.routeNativeElement?.tagName.toLowerCase()).toBe('app-conversation-probe');
    service.track('a');
    offset = 320;

    const forwardDone = nextNavigationEnd();
    location.forward();
    await forwardDone;

    expect(service.get('a')).toBe(320);
  });

  it('keeps a remembered offset when the user leaves before the conversation was positioned', async () => {
    service.save('a', 300);
    await harness.navigateByUrl('/s/a');
    // Still loading: reset to the top, not yet restored, so not tracked.

    await harness.navigateByUrl('/customize/tools');

    expect(service.get('a')).toBe(300);
  });

  it('records nothing for a conversation no longer tracked', async () => {
    await harness.navigateByUrl('/s/a');
    service.track('a');
    service.untrack();
    offset = 200;

    await harness.navigateByUrl('/customize/tools');

    expect(service.get('a')).toBeUndefined();
  });

  it('does not record another page as the conversation once untracked', async () => {
    await harness.navigateByUrl('/s/a');
    service.track('a');
    offset = 200;
    await harness.navigateByUrl('/customize/tools');
    service.untrack(); // the conversation page's ngOnDestroy

    await harness.navigateByUrl('/s/b');

    expect(service.get('a')).toBe(200);
  });
});
