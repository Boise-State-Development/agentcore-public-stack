/**
 * Recognise the error a browser raises when a lazy chunk cannot be fetched.
 *
 * The usual cause is a deploy: `scripts/frontend/deploy.sh` syncs with
 * `--delete`, so the previous build's hashed chunks are gone the moment the
 * new build lands. A tab opened before that still holds the old `main` bundle,
 * whose lazy routes name chunks S3 no longer has (CloudFront answers 403), and
 * the next navigation to one of them rejects its dynamic `import()`.
 *
 * There is no error class to test for — each engine words the rejection its
 * own way, so this matches on the message:
 *
 * | Engine          | Message                                                    |
 * |-----------------|------------------------------------------------------------|
 * | Chromium        | `Failed to fetch dynamically imported module: <url>`       |
 * | Safari / WebKit | `Importing a module script failed.`                        |
 * | Firefox         | `error loading dynamically imported module: <url>`         |
 * | webpack         | `ChunkLoadError` / `Loading chunk 123 failed.`             |
 *
 * The webpack forms cannot come from our own esbuild output; they are matched
 * so a dependency that ships its own webpack runtime is recovered the same way.
 * `Expected a JavaScript module script` covers a server that answers a missing
 * chunk with the SPA shell (an HTML 200) instead of a 403/404.
 */
const CHUNK_LOAD_MESSAGE_PATTERNS: readonly RegExp[] = [
  /failed to fetch dynamically imported module/i,
  /importing a module script failed/i,
  /error loading dynamically imported module/i,
  /loading (css )?chunk [\w-]+ failed/i,
  /expected a javascript(-or-wasm)? module script/i,
];

/**
 * How far to follow wrappers (`cause`, zone.js's `rejection`, `error`) before
 * giving up. Real wrapping is one or two levels; the bound only stops a
 * self-referencing object from spinning forever.
 */
const MAX_UNWRAP_DEPTH = 4;

/** Whether `error`, or an error it wraps, is a failed lazy-chunk load. */
export function isChunkLoadError(error: unknown, depth = 0): boolean {
  if (error == null || depth > MAX_UNWRAP_DEPTH) return false;

  if (typeof error === 'string') return matchesChunkLoadMessage(error);
  if (typeof error !== 'object') return false;

  const candidate = error as { name?: unknown; message?: unknown };
  if (candidate.name === 'ChunkLoadError') return true;
  if (typeof candidate.message === 'string' && matchesChunkLoadMessage(candidate.message)) {
    return true;
  }

  const wrapper = error as { cause?: unknown; rejection?: unknown; error?: unknown };
  return (
    isChunkLoadError(wrapper.cause, depth + 1) ||
    isChunkLoadError(wrapper.rejection, depth + 1) ||
    isChunkLoadError(wrapper.error, depth + 1)
  );
}

function matchesChunkLoadMessage(message: string): boolean {
  return CHUNK_LOAD_MESSAGE_PATTERNS.some(pattern => pattern.test(message));
}
