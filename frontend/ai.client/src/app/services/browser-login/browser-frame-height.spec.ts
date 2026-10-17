import { describe, expect, it } from 'vitest';

import { BROWSER_PANEL_CHROME_PX, browserFrameHeight } from './browser-frame-height';

describe('browserFrameHeight', () => {
  it('is the window height less the panel chrome', () => {
    // The 1440x900 measurement the constant came from: 778 px of frame.
    expect(browserFrameHeight({ innerHeight: 900 })).toBe(778);
    expect(browserFrameHeight({ innerHeight: 1200 })).toBe(1200 - BROWSER_PANEL_CHROME_PX);
  });

  it('rounds a fractional (zoomed) height down, never up past the panel', () => {
    expect(browserFrameHeight({ innerHeight: 900.6 })).toBe(778);
  });

  it('is null when there is nothing usable to measure', () => {
    expect(browserFrameHeight(null)).toBeNull();
    expect(browserFrameHeight({ innerHeight: Number.NaN })).toBeNull();
    expect(browserFrameHeight({ innerHeight: BROWSER_PANEL_CHROME_PX })).toBeNull();
  });
});
