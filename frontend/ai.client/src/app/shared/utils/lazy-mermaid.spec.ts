import { describe, it, expect, vi } from 'vitest';
import { installLazyMermaid, type MermaidHost, type MermaidImporter } from './lazy-mermaid';

/**
 * The stand-in has to satisfy ngx-markdown's contract exactly: it resolves
 * `mermaid` off the global scope on every render, throws unless
 * `typeof mermaid.initialize` is a function, and then calls
 * `initialize(config)` followed by `run({ nodes })`.
 */

function fakeMermaid() {
  return {
    initialize: vi.fn(),
    run: vi.fn().mockResolvedValue(undefined),
  };
}

function hostWithLoader(mermaid = fakeMermaid()) {
  const host = {} as MermaidHost;
  const load = vi.fn().mockResolvedValue({ default: mermaid });
  installLazyMermaid(host, load as unknown as MermaidImporter);
  return { host, load, mermaid };
}

describe('installLazyMermaid', () => {
  it('publishes a global that satisfies ngx-markdown without loading mermaid', () => {
    const { host, load } = hostWithLoader();

    expect(typeof host.mermaid?.initialize).toBe('function');
    expect(load).not.toHaveBeenCalled();
  });

  it('loads the real library on the first run() and replays the recorded config', async () => {
    const { host, load, mermaid } = hostWithLoader();
    const nodes = [] as unknown as NodeListOf<HTMLElement>;

    host.mermaid!.initialize({ startOnLoad: false, theme: 'dark' });
    await host.mermaid!.run({ nodes });

    expect(load).toHaveBeenCalledTimes(1);
    expect(mermaid.initialize).toHaveBeenCalledWith({ startOnLoad: false, theme: 'dark' });
    expect(mermaid.run).toHaveBeenCalledWith({ nodes });
  });

  it('hands the real library to the global so later renders skip the stand-in', async () => {
    const { host, mermaid } = hostWithLoader();

    const standIn = host.mermaid;
    await host.mermaid!.run({ nodes: [] as unknown as NodeListOf<HTMLElement> });

    expect(host.mermaid).toBe(mermaid);
    expect(host.mermaid).not.toBe(standIn);
  });

  it('imports once when renders overlap while the chunk is still in flight', async () => {
    const { host, load } = hostWithLoader();
    const nodes = [] as unknown as NodeListOf<HTMLElement>;

    await Promise.all([host.mermaid!.run({ nodes }), host.mermaid!.run({ nodes })]);

    expect(load).toHaveBeenCalledTimes(1);
  });

  it('swallows a failed chunk load so one bad diagram cannot break the message', async () => {
    const host = {} as MermaidHost;
    const load = vi.fn().mockRejectedValue(new Error('network'));
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    installLazyMermaid(host, load as unknown as MermaidImporter);

    await expect(
      host.mermaid!.run({ nodes: [] as unknown as NodeListOf<HTMLElement> }),
    ).resolves.toBeUndefined();
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it('leaves an already-present mermaid alone', () => {
    const existing = fakeMermaid();
    const host = { mermaid: existing } as unknown as MermaidHost;

    installLazyMermaid(host, vi.fn() as unknown as MermaidImporter);

    expect(host.mermaid).toBe(existing);
  });
});
