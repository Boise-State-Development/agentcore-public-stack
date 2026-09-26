import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { InViewDirective } from './in-view.directive';

@Component({
  imports: [InViewDirective],
  template: `
    <div style="overflow-y: auto">
      <div appInView [appInViewMargin]="240" [appInViewRemeasure]="remeasure()" (inView)="seen.push($event)"></div>
    </div>
  `,
})
class HostComponent {
  readonly remeasure = signal(0);
  readonly seen: boolean[] = [];
}

describe('InViewDirective', () => {
  let observers: { root: Element | null; rootMargin: string; observe: ReturnType<typeof vi.fn>; unobserve: ReturnType<typeof vi.fn>; report: (visible: boolean) => void }[];

  beforeEach(() => {
    observers = [];
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        observe = vi.fn();
        unobserve = vi.fn();
        disconnect = vi.fn();
        root: Element | null;
        rootMargin: string;
        constructor(cb: (entries: { isIntersecting: boolean }[]) => void, init: IntersectionObserverInit) {
          this.root = (init.root as Element) ?? null;
          this.rootMargin = init.rootMargin ?? '';
          observers.push({ ...this, observe: this.observe, unobserve: this.unobserve, report: (v) => cb([{ isIntersecting: v }]) });
        }
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  it('observes against the nearest scrolling ancestor, with the margin below it', () => {
    const fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();

    expect(observers).toHaveLength(1);
    expect(observers[0].root).toBe(fixture.nativeElement.firstElementChild);
    expect(observers[0].rootMargin).toBe('0px 0px 240px 0px');
  });

  it('emits what the observer reports', () => {
    const fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();

    observers[0].report(true);
    observers[0].report(false);
    expect(fixture.componentInstance.seen).toEqual([true, false]);
  });

  it('re-observes on remeasure, so a host that stayed in view reports again', () => {
    const fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();
    const calls = observers[0].observe.mock.calls.length;

    fixture.componentInstance.remeasure.set(1);
    fixture.detectChanges();

    expect(observers[0].unobserve).toHaveBeenCalled();
    expect(observers[0].observe.mock.calls.length).toBe(calls + 1);
  });
});
