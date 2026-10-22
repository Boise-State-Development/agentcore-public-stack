import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ModelIconComponent, ModelIconSize } from './model-icon.component';
import { resolveModelIcon } from '../../admin/manage-models/models/model-icons';
import { ConfigService } from '../../services/config.service';

type IconModel = {
  iconUrl?: string | null;
  iconSlug?: string | null;
  providerName: string;
  modelName?: string;
};

describe('resolveModelIcon', () => {
  it('prefers an uploaded icon over a built-in slug', () => {
    // The more specific, more deliberate act wins: an admin who uploaded a file
    // after picking a logo meant the file.
    const icon = resolveModelIcon({
      iconUrl: '/models/m-1/icon?v=abc',
      iconSlug: 'anthropic',
      providerName: 'Anthropic',
    });

    expect(icon).toEqual({ kind: 'upload', url: '/models/m-1/icon?v=abc' });
  });

  it('prefers an explicit slug over both guesses', () => {
    const icon = resolveModelIcon({
      iconSlug: 'meta',
      providerName: 'Anthropic',
      modelId: 'us.anthropic.claude-sonnet-5',
    });

    expect(icon).toEqual({ kind: 'builtin', slug: 'meta', via: 'slug' });
  });

  it('prefers the model-family mark over the company mark', () => {
    // The rule: a vendor that brands its models separately shows that mark.
    // Every Anthropic model we serve is a Claude, so the starburst wins over
    // the corporate 'A' even though the provider name matches it too.
    expect(
      resolveModelIcon({ providerName: 'Anthropic', modelId: 'us.anthropic.claude-sonnet-4-6' }),
    ).toEqual({ kind: 'builtin', slug: 'claude', via: 'model' });

    // The CRIS prefix varies per environment and must not change the answer.
    expect(
      resolveModelIcon({ providerName: 'Anthropic', modelId: 'global.anthropic.claude-sonnet-5' }),
    ).toEqual({ kind: 'builtin', slug: 'claude', via: 'model' });

    expect(
      resolveModelIcon({ providerName: 'Moonshot AI', modelId: 'us.moonshotai.kimi-k3' }),
    ).toEqual({ kind: 'builtin', slug: 'kimi', via: 'model' });

    expect(
      resolveModelIcon({ providerName: 'Qwen', modelId: 'qwen.qwen3-coder-30b-a3b-instruct' }),
    ).toEqual({ kind: 'builtin', slug: 'qwen', via: 'model' });
  });

  it('falls back to the company mark when the family has none of its own', () => {
    // OpenAI publishes one mark for the whole GPT fleet — there is no
    // per-model logo to prefer, so the company one is the right answer.
    expect(
      resolveModelIcon({ providerName: 'OpenAI', modelId: 'us.openai.gpt-5.6-sol' }),
    ).toEqual({ kind: 'builtin', slug: 'openai', via: 'provider' });
  });

  it('falls back to matching the provider name', () => {
    expect(resolveModelIcon({ providerName: 'Anthropic' })).toEqual({
      kind: 'builtin',
      slug: 'anthropic',
      via: 'provider',
    });
    // Case and padding come from a free-text field an admin typed.
    expect(resolveModelIcon({ providerName: '  openai ' })).toEqual({
      kind: 'builtin',
      slug: 'openai',
      via: 'provider',
    });
  });

  it('serves Gemma the Google mark, because its own is illegible at tile size', () => {
    // The one deliberate exception to model-beats-company. Pinned so a future
    // "Gemma has a logo, why aren't we using it?" change has to read the why.
    expect(
      resolveModelIcon({ providerName: 'Google', modelId: 'google.gemma-4-27b-it' }),
    ).toEqual({ kind: 'builtin', slug: 'google', via: 'model' });

    // Resolves even when the provider name is something the map doesn't carry.
    expect(
      resolveModelIcon({ providerName: 'Google DeepMind Research', modelId: 'google.gemma-4-9b' }),
    ).toEqual({ kind: 'builtin', slug: 'google', via: 'model' });
  });

  it('keeps the company mark for an Anthropic model that is not a Claude', () => {
    // Why 'anthropic' survives as a slug rather than being replaced outright.
    expect(
      resolveModelIcon({ providerName: 'Anthropic', modelId: 'us.anthropic.some-future-model' }),
    ).toEqual({ kind: 'builtin', slug: 'anthropic', via: 'provider' });
  });

  it('resolves to nothing for a provider we ship no logo for', () => {
    expect(resolveModelIcon({ providerName: 'Acme Labs' })).toEqual({ kind: 'none' });
  });

  it('ignores a slug we ship no asset for rather than pointing at a missing file', () => {
    // The backend validates on write, so this only happens to a hand-edited
    // record — but a 404'd <img> is worse than the provider fallback.
    expect(resolveModelIcon({ iconSlug: 'acme', providerName: 'Acme Labs' })).toEqual({
      kind: 'none',
    });
  });
});

describe('ModelIconComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    TestBed.inject(ConfigService).appApiUrl.set('/api');
  });

  afterEach(() => TestBed.resetTestingModule());

  function create(model: IconModel, inputs: { size?: ModelIconSize; alt?: string } = {}) {
    const fixture = TestBed.createComponent(ModelIconComponent);
    fixture.componentRef.setInput('model', model);
    if (inputs.size !== undefined) fixture.componentRef.setInput('size', inputs.size);
    if (inputs.alt !== undefined) fixture.componentRef.setInput('alt', inputs.alt);
    fixture.detectChanges();
    return fixture;
  }

  it('prefixes the API base onto the relative upload path', () => {
    // The API hands out a relative path so the container never has to know its
    // own public origin.
    const fixture = create({ iconUrl: '/models/m-1/icon?v=abc', providerName: 'Acme' });

    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');
    expect(img.getAttribute('src')).toBe('/api/models/m-1/icon?v=abc');
  });

  it('leaves an already-absolute URL alone, so a local blob preview still renders', () => {
    const fixture = create({ iconUrl: 'blob:http://localhost/abc', providerName: 'Acme' });

    const img: HTMLImageElement = fixture.nativeElement.querySelector('img');
    expect(img.getAttribute('src')).toBe('blob:http://localhost/abc');
  });

  it('renders both halves of a built-in logo pair so theme switching is pure CSS', () => {
    const fixture = create({ iconSlug: 'anthropic', providerName: 'Anthropic' });

    const sources = Array.from(
      fixture.nativeElement.querySelectorAll('img') as NodeListOf<HTMLImageElement>,
    ).map(img => img.getAttribute('src'));
    expect(sources).toEqual([
      '/img/provider-logos/anthropic/light.svg',
      '/img/provider-logos/anthropic/dark.svg',
    ]);
  });

  it('draws a monogram when nothing resolves, rather than an empty tile', () => {
    const fixture = create({ providerName: 'Acme Labs', modelName: 'Zephyr 2' });

    expect(fixture.nativeElement.querySelector('img')).toBeNull();
    expect(fixture.nativeElement.textContent.trim()).toBe('Z');
  });

  it('falls back to the provider initial when the model has no name', () => {
    const fixture = create({ providerName: 'Acme Labs' });

    expect(fixture.nativeElement.textContent.trim()).toBe('A');
  });

  it('falls back to the monogram when the uploaded icon fails to load', () => {
    // The backend answers 404 for a key that outlived its object, and a menu row
    // has to stay composed.
    const fixture = create({
      iconUrl: '/models/m-1/icon?v=abc',
      providerName: 'Acme Labs',
      modelName: 'Zephyr 2',
    });

    fixture.componentInstance.failed.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('img')).toBeNull();
    expect(fixture.nativeElement.textContent.trim()).toBe('Z');
  });

  it('is decorative by default and labelled only when asked', () => {
    const plain = create({ providerName: 'Acme Labs' });
    expect(plain.nativeElement.querySelector('span').getAttribute('aria-hidden')).toBe('true');

    const labelled = create({ providerName: 'Acme Labs' }, { alt: 'Icon preview' });
    const tile: HTMLElement = labelled.nativeElement.querySelector('span');
    expect(tile.getAttribute('role')).toBe('img');
    expect(tile.getAttribute('aria-label')).toBe('Icon preview');
  });
});
