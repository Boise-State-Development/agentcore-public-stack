import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { AgentIconComponent, gradientFor } from './agent-icon.component';
import { ConfigService } from '../../services/config.service';

describe('AgentIconComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    TestBed.inject(ConfigService).appApiUrl.set('/api');
  });

  afterEach(() => TestBed.resetTestingModule());

  function create(inputs: {
    agentId: string;
    iconUrl?: string;
    emoji?: string;
    size?: 28 | 40 | 52 | 84;
    alt?: string;
  }) {
    const fixture = TestBed.createComponent(AgentIconComponent);
    fixture.componentRef.setInput('agentId', inputs.agentId);
    if (inputs.iconUrl !== undefined) fixture.componentRef.setInput('iconUrl', inputs.iconUrl);
    if (inputs.emoji !== undefined) fixture.componentRef.setInput('emoji', inputs.emoji);
    if (inputs.size !== undefined) fixture.componentRef.setInput('size', inputs.size);
    if (inputs.alt !== undefined) fixture.componentRef.setInput('alt', inputs.alt);
    fixture.detectChanges();
    return fixture;
  }

  // ── the generated fallback (D5) ────────────────────────────────────────────────
  it('draws the generated gradient when there is no uploaded icon', () => {
    const fixture = create({ agentId: 'ast-1', emoji: '📋' });

    const tile: HTMLElement = fixture.nativeElement.querySelector('span');
    expect(fixture.nativeElement.querySelector('img')).toBeNull();
    expect(tile.style.backgroundImage).toContain('linear-gradient');
    expect(fixture.nativeElement.textContent).toContain('📋');
  });

  it('falls back to a glyph when the agent has no emoji either', () => {
    const fixture = create({ agentId: 'ast-1' });
    expect(fixture.nativeElement.textContent).toContain('✦');
  });

  it('draws the same gradient for the same agent everywhere', () => {
    // The store, My Agents and the chat header must not look like three agents.
    const a = create({ agentId: 'ast-42', size: 84 });
    const b = create({ agentId: 'ast-42', size: 28 });

    const gradient = (fixture: ReturnType<typeof create>) =>
      (fixture.nativeElement.querySelector('span') as HTMLElement).style.backgroundImage;
    // The DOM re-serializes the hex pair as rgb(), so the rendered values are compared
    // to each other and the pure function is pinned separately.
    expect(gradient(a)).toBe(gradient(b));
    expect(gradientFor('ast-42')).toBe(gradientFor('ast-42'));
    expect(gradientFor('ast-42')).toMatch(/^linear-gradient\(135deg, #[0-9a-f]{6}, #[0-9a-f]{6}\)$/);
  });

  it('spreads different agents across the palette', () => {
    const drawn = new Set(
      Array.from({ length: 40 }, (_, i) => gradientFor(`ast-${i}`)),
    );
    // Not a distribution proof — a guard against a hash that collapses to one tile.
    expect(drawn.size).toBeGreaterThan(5);
  });

  // ── the uploaded icon ──────────────────────────────────────────────────────────
  it('prefixes the API base onto the relative path the read shape carries', () => {
    const fixture = create({ agentId: 'ast-1', iconUrl: '/agents/ast-1/icon?v=abc123' });

    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');
    expect(img.getAttribute('src')).toBe('/api/agents/ast-1/icon?v=abc123');
  });

  it('leaves an absolute URL alone, so the upload dialog can preview a local file', () => {
    const fixture = create({ agentId: 'ast-1', iconUrl: 'blob:http://localhost/abcd' });

    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');
    expect(img.getAttribute('src')).toBe('blob:http://localhost/abcd');
  });

  it('drops to the gradient when the image fails to load', () => {
    // The backend answers 404 for a key that outlived its object; that path has to
    // land on the designed default rather than a broken tile.
    const fixture = create({ agentId: 'ast-1', iconUrl: '/agents/ast-1/icon?v=gone', emoji: '📋' });
    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');

    img.dispatchEvent(new Event('error'));
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('img')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('📋');
  });

  it('re-attempts the load when the icon is replaced', () => {
    const fixture = create({ agentId: 'ast-1', iconUrl: '/agents/ast-1/icon?v=gone' });
    fixture.nativeElement.querySelector('img').dispatchEvent(new Event('error'));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('img')).toBeNull();

    fixture.componentRef.setInput('iconUrl', '/agents/ast-1/icon?v=fresh');
    fixture.detectChanges();

    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');
    expect(img.getAttribute('src')).toBe('/api/agents/ast-1/icon?v=fresh');
  });

  // ── the four sizes (D5) ────────────────────────────────────────────────────────
  it.each([
    [28, 'size-7'],
    [40, 'size-10'],
    [52, 'size-13'],
    [84, 'size-21'],
  ] as const)('renders %ipx as %s', (size, expected) => {
    const fixture = create({ agentId: 'ast-1', size });
    const tile: HTMLElement = fixture.nativeElement.querySelector('span');
    expect(tile.className).toContain(expected);
  });

  // ── accessibility ──────────────────────────────────────────────────────────────
  it('is decorative by default, since rows already name the agent', () => {
    const fixture = create({ agentId: 'ast-1' });
    const tile: HTMLElement = fixture.nativeElement.querySelector('span');
    expect(tile.getAttribute('aria-hidden')).toBe('true');
    expect(tile.getAttribute('role')).toBeNull();
  });

  it('becomes an image with a label when given alt text', () => {
    const fixture = create({ agentId: 'ast-1', alt: 'Policy Lookup' });
    const tile: HTMLElement = fixture.nativeElement.querySelector('span');
    expect(tile.getAttribute('role')).toBe('img');
    expect(tile.getAttribute('aria-label')).toBe('Policy Lookup');
    expect(tile.getAttribute('aria-hidden')).toBeNull();
  });
});
