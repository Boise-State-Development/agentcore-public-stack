import { AfterViewInit, Directive, ElementRef, OnDestroy, effect, inject, input, output } from '@angular/core';

/**
 * Emits whether the host is within `margin` pixels of being visible in its
 * nearest scrolling ancestor.
 *
 * Observes against that ancestor rather than the viewport: the viewport's
 * margin cannot see past the ancestor's clipping, so a viewport-rooted
 * observer only fires once the host is actually on screen and the margin
 * would buy no head start.
 *
 * The observer reports only when the host crosses the edge, so a host that
 * stays in view goes quiet. Change `appInViewRemeasure` to ask again: the host
 * is re-observed and reports its current state once the page has laid out.
 */
@Directive({ selector: '[appInView]' })
export class InViewDirective implements AfterViewInit, OnDestroy {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** How far below the visible area the host still counts as in view. */
  readonly margin = input(0, { alias: 'appInViewMargin' });

  /** Any value; each change re-reports whether the host is in view. */
  readonly remeasure = input<unknown>(undefined, { alias: 'appInViewRemeasure' });

  readonly inView = output<boolean>();

  private observer?: IntersectionObserver;

  constructor() {
    effect(() => {
      this.remeasure();
      const el = this.host.nativeElement;
      // A fresh `observe` always delivers an initial entry, measured after layout.
      this.observer?.unobserve(el);
      this.observer?.observe(el);
    });
  }

  ngAfterViewInit(): void {
    // Guarded rather than assumed, so a spec that renders the host under jsdom
    // doesn't throw. Without an observer the host simply never reports in view.
    if (typeof IntersectionObserver === 'undefined') return;

    const el = this.host.nativeElement;
    this.observer = new IntersectionObserver(
      (entries) => this.inView.emit(entries[entries.length - 1].isIntersecting),
      { root: scrollParent(el), rootMargin: `0px 0px ${this.margin()}px 0px` },
    );
    this.observer.observe(el);
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
  }
}

function scrollParent(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const overflowY = getComputedStyle(node).overflowY;
    if (overflowY === 'auto' || overflowY === 'scroll') return node;
  }
  return null;
}
