import { describe, it, expect, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ContextTrajectoryChartComponent } from './context-trajectory-chart.component';
import { ContextTrajectoryPoint } from '../models';

function points(n: number, tokens: (i: number) => number): ContextTrajectoryPoint[] {
  return Array.from({ length: n }, (_, i) => ({
    callIndex: i,
    timestamp: `2026-09-02T00:00:${String(i).padStart(2, '0')}Z`,
    contextTokens: tokens(i),
    cacheStatus: i === 0 ? 'first_write' : 'hit',
    modelId: 'm',
    cost: 0.01,
  }));
}

describe('ContextTrajectoryChartComponent', () => {
  function setup(pts: ContextTrajectoryPoint[], threshold = 100_000, window: number | null = 200_000) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(ContextTrajectoryChartComponent);
    fixture.componentRef.setInput('points', pts);
    fixture.componentRef.setInput('threshold', threshold);
    fixture.componentRef.setInput('contextWindow', window);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('renders one column per call, capped at 24px with a surface gap', () => {
    const fixture = setup(points(5, (i) => 10_000 * (i + 1)));
    // The chart SVG is the figure's direct child; legend swatches are SVGs of their own.
    const rects = fixture.nativeElement.querySelectorAll('figure > svg rect');
    expect(rects.length).toBe(5);
    expect(fixture.componentInstance.barWidth()).toBe(24);
    // Many calls: the column shrinks to fit its slot but never below 2px.
    const dense = setup(points(400, () => 1_000));
    expect(dense.componentInstance.barWidth()).toBeGreaterThanOrEqual(2);
    expect(dense.componentInstance.barWidth()).toBeLessThan(24);
  });

  it('always draws the compaction threshold and labels it', () => {
    const fixture = setup(points(2, () => 5_000));
    const text: string = fixture.nativeElement.textContent;
    expect(text).toContain('compaction threshold 100K');
    // Window is far above the data → named in the caption, not drawn.
    expect(text).toContain('window 200K');
  });

  it('draws a legend only when more than one cache status is present', () => {
    const single = setup([{ callIndex: 0, timestamp: 't', contextTokens: 100, cacheStatus: 'hit' }]);
    expect(single.nativeElement.querySelector('ul[aria-label="Cache status legend"]')).toBeNull();
    const multi = setup(points(3, () => 100));
    expect(multi.nativeElement.querySelector('ul[aria-label="Cache status legend"]')).not.toBeNull();
  });

  it('exposes the numbers to assistive tech and hover', () => {
    const fixture = setup(points(3, (i) => 1_000 * (i + 1)));
    const svg = fixture.nativeElement.querySelector('svg');
    expect(svg.getAttribute('aria-label')).toContain('3 calls, peak 3.0K');
    const title = fixture.nativeElement.querySelector('figure > svg rect title').textContent;
    expect(title).toContain('Call 1: 1,000 tokens in context');
    expect(title).toContain('cache: first_write');
  });

  it('says so when there are no calls', () => {
    const fixture = setup([]);
    expect(fixture.nativeElement.textContent).toContain('No model calls recorded');
  });
});
