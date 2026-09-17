import { CdkVirtualScrollViewport } from '@angular/cdk/scrolling';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DataGridComponent } from './data-grid.component';

function rows(n: number, cols = 2): string[][] {
  return Array.from({ length: n }, (_, i) =>
    Array.from({ length: cols }, (_, c) => `r${i}c${c}`),
  );
}

describe('DataGridComponent', () => {
  let fixture: ComponentFixture<DataGridComponent>;

  beforeEach(async () => {
    // jsdom implements neither Element.scrollTo nor Element.scrollBy,
    // and CDK's `scrollToOffset` ends in the former. A no-op keeps the
    // component's real code path intact; the scrolling itself is
    // verified in a browser, where jsdom's gaps do not apply.
    for (const name of ['scrollTo', 'scrollBy'] as const) {
      if (typeof (Element.prototype as never as Record<string, unknown>)[name] !== 'function') {
        (Element.prototype as never as Record<string, unknown>)[name] = () => undefined;
      }
    }

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [DataGridComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(DataGridComponent);
  });

  function render(headers: string[], body: string[][]): HTMLElement {
    fixture.componentRef.setInput('headers', headers);
    fixture.componentRef.setInput('rows', body);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('renders the headers it is given', () => {
    const el = render(['Item', 'Cost'], [['Rent', '1200']]);

    const headers = Array.from(el.querySelectorAll('[role="columnheader"]'))
      .map((n) => n.textContent?.trim())
      .filter((t) => t !== '#');
    expect(headers).toEqual(['Item', 'Cost']);
  });

  it('reports the data size to assistive tech, not the rendered window', () => {
    const el = render(['a', 'b'], rows(900));

    const grid = el.querySelector('[role="grid"]');
    expect(grid?.getAttribute('aria-rowcount')).toBe('901');
    expect(grid?.getAttribute('aria-colcount')).toBe('2');
  });

  it('shows the empty message in place of the body when there are no rows', () => {
    fixture.componentRef.setInput('emptyMessage', 'This sheet is empty.');
    const el = render(['a', 'b'], []);

    expect(el.textContent).toContain('This sheet is empty.');
    expect(el.querySelector('cdk-virtual-scroll-viewport')).toBeNull();
  });

  it('gives header and body rows one shared track list', () => {
    const el = render(['a', 'b'], [['1', '2']]);

    const grid = el.querySelector('[role="grid"]') as HTMLElement;
    // Gutter plus one track per column.
    expect(
      grid.style.getPropertyValue('--grid-cols').trim().split(/\s+/),
    ).toHaveLength(3);
  });

  describe('re-arming the scroller when the data changes', () => {
    /**
     * One viewport instance serves every dataset — switching worksheet
     * tabs, or previewing a second file without closing the pane. CDK
     * picks up the new length but does NOT recompute the rendered
     * range, so the body stays pinned to the first screenful while the
     * scrollbar moves over the full height. Verified in the browser: at
     * scrollOffset 10000 of a 1,200-row sheet the rendered range was
     * still {0, 32}, and checkViewportSize() corrected it to {309, 345}.
     *
     * jsdom gives the viewport zero height, so no rows are ever
     * rendered and the range itself cannot be asserted here. What this
     * does pin down is that the component asks the viewport to
     * re-measure and rewind — the two calls whose absence caused it.
     */
    function spyOnViewport() {
      const viewport = fixture.debugElement
        .query((n) => n.componentInstance instanceof CdkVirtualScrollViewport)
        ?.componentInstance as CdkVirtualScrollViewport;
      expect(viewport).toBeDefined();
      return {
        checkViewportSize: vi.spyOn(viewport, 'checkViewportSize'),
        scrollToOffset: vi.spyOn(viewport, 'scrollToOffset'),
      };
    }

    it('re-measures and rewinds when the rows are replaced', () => {
      vi.useFakeTimers();
      try {
        render(['a', 'b'], rows(5));
        const spies = spyOnViewport();

        render(['a', 'b'], rows(1200));
        vi.runAllTimers();

        expect(spies.checkViewportSize).toHaveBeenCalled();
        expect(spies.scrollToOffset).toHaveBeenCalledWith(0);
      } finally {
        vi.useRealTimers();
      }
    });

    it('rewinds the header to match', () => {
      vi.useFakeTimers();
      try {
        const el = render(['a', 'b'], rows(5));
        const header = el.querySelector('[role="grid"] > div') as HTMLElement;
        header.scrollLeft = 250;

        render(['a', 'b'], rows(1200));
        vi.runAllTimers();

        expect(header.scrollLeft).toBe(0);
      } finally {
        vi.useRealTimers();
      }
    });
  });
});
