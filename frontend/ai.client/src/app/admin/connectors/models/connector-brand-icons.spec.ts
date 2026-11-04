import { describe, it, expect } from 'vitest';
import { CONNECTOR_BRAND_ICONS, getBrandIconDataUrl } from './connector-brand-icons';
import { CONNECTOR_PRESETS } from './connector.model';

// The backend accepts this shape and no other (`validate_icon_data`), capped at 100KB.
const BACKEND_ICON_DATA = /^data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+$/;

describe('connector brand icons', () => {
  const entries = Object.entries(CONNECTOR_BRAND_ICONS);

  it('only covers types that have a preset', () => {
    const presetTypes = new Set<string>(CONNECTOR_PRESETS.map(p => p.type));
    for (const [type] of entries) {
      expect(presetTypes.has(type)).toBe(true);
    }
  });

  it.each(entries)('%s is a self-contained SVG', (_type, icon) => {
    expect(icon.svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="')).toBe(true);
    // The size comes from the tile, not the file.
    expect(icon.svg.match(/^<svg[^>]*>/)![0]).not.toMatch(/\s(width|height)=/);
    // Rendered in an <img>, so scripts could not run, but nothing active has any business here.
    expect(icon.svg).not.toMatch(/<script|<style|<image|foreignObject|<!--|<\?xml|\son\w+=/i);
    // Gradients reference local ids only; nothing is fetched.
    for (const [, href] of icon.svg.matchAll(/href="([^"]*)"/g)) {
      expect(href.startsWith('#')).toBe(true);
    }
  });

  it.each(entries)('%s names a known source', (_type, icon) => {
    expect(['logos', 'thesvg', 'commons']).toContain(icon.source);
  });

  it.each(entries)('%s produces a data URL the backend accepts', type => {
    const url = getBrandIconDataUrl(type as never);
    expect(url).toMatch(BACKEND_ICON_DATA);
    expect(url!.length).toBeLessThan(100 * 1024);
  });

  it('round-trips the SVG through the data URL', () => {
    const url = getBrandIconDataUrl('github')!;
    expect(atob(url.split(',')[1])).toBe(CONNECTOR_BRAND_ICONS['github']!.svg);
  });

  it('has no logo for brands that publish only a wordmark', () => {
    expect(getBrandIconDataUrl('onelogin')).toBeNull();
    expect(getBrandIconDataUrl('ping')).toBeNull();
    expect(getBrandIconDataUrl('custom')).toBeNull();
  });

  it('covers every built-in vendor except the wordmark-only ones', () => {
    const wordmarkOnly = new Set(['onelogin', 'ping', 'custom']);
    for (const preset of CONNECTOR_PRESETS) {
      if (wordmarkOnly.has(preset.type)) continue;
      expect(getBrandIconDataUrl(preset.type), preset.type).not.toBeNull();
    }
  });

  it('returns the same URL on repeat calls', () => {
    expect(getBrandIconDataUrl('zoom')).toBe(getBrandIconDataUrl('zoom'));
  });
});
