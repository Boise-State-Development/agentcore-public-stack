/**
 * Lazily supplies the global `mermaid` object that ngx-markdown's `mermaid`
 * plugin resolves against.
 *
 * ngx-markdown reads mermaid off the global scope — `renderMermaid()` throws
 * `errorMermaidNotLoaded` when `typeof mermaid === 'undefined'` — and its
 * README tells you to satisfy that by listing `mermaid.min.js` in the
 * `scripts` array in `angular.json`. Entries in that array are emitted as a
 * plain `<script>` tag on `index.html`, *outside* the module graph, so no
 * amount of lazy routing or `@defer` in the app can reach them: they are eager
 * for every visitor on every cold load. Mermaid is 3.57 MB raw / ~1 MB gzip —
 * 92% of the `scripts` bundle and 71% of the entire initial bundle, paid by
 * everyone, for a feature that only renders when an assistant message happens
 * to contain a ```mermaid fence.
 *
 * So we install a stand-in exposing only the two methods ngx-markdown calls.
 * `initialize()` just records the config. `run()` — which ngx-markdown reaches
 * only once the rendered DOM actually contains a `.mermaid` element — imports
 * the real library into its own lazy chunk, replays the recorded config, and
 * publishes the real module as the global so every later render goes straight
 * to it.
 */

type MermaidApi = (typeof import('mermaid'))['default'];
type MermaidConfig = Parameters<MermaidApi['initialize']>[0];
type MermaidRunOptions = Parameters<MermaidApi['run']>[0];

/** The slice of mermaid's surface ngx-markdown actually touches. */
interface MermaidStandIn {
  initialize(config: MermaidConfig): void;
  run(options?: MermaidRunOptions): Promise<void>;
}

/** Whatever object carries the `mermaid` global — `globalThis` in the app. */
export type MermaidHost = typeof globalThis & { mermaid?: MermaidApi | MermaidStandIn };

/** Seam for specs — the default pulls mermaid into its own lazy chunk. */
export type MermaidImporter = () => Promise<{ default: MermaidApi }>;

const importMermaid: MermaidImporter = () => import('mermaid');

/**
 * Publish the stand-in on the global scope. A no-op when a real mermaid is
 * already there, so it stays safe to call twice.
 */
export function installLazyMermaid(
  host: MermaidHost = globalThis as MermaidHost,
  load: MermaidImporter = importMermaid,
): void {
  if (host.mermaid) {
    return;
  }

  let pending: Promise<MermaidApi> | null = null;
  let lastConfig: MermaidConfig | undefined;

  const standIn: MermaidStandIn = {
    initialize(config: MermaidConfig): void {
      lastConfig = config;
    },
    run(options?: MermaidRunOptions): Promise<void> {
      pending ??= load().then(({ default: mermaid }) => {
        // Hand the real library over. ngx-markdown resolves `mermaid` afresh
        // on every render, so from here on it bypasses the stand-in entirely.
        host.mermaid = mermaid;
        return mermaid;
      });

      return pending
        .then((mermaid) => {
          // Mirror the eager ordering: ngx-markdown always initializes
          // immediately before it runs.
          if (lastConfig) {
            mermaid.initialize(lastConfig);
          }
          return mermaid.run(options);
        })
        .catch((error: unknown) => {
          console.error('[lazy-mermaid] diagram rendering unavailable', error);
        });
    },
  };

  host.mermaid = standIn;
}
