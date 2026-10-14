// source-tree.ts — the one directory walk behind the whole-tree guard specs.
//
// A handful of specs (color-tokens, surface-literal-guard,
// tailwind-theme-import-guard, inline-template-backtick-guard) are lint
// rules written as tests: they read every component source under src/ or
// src/app and fail on a banned pattern. Each used to carry its own
// recursive readdirSync, and each tested its skip rules against every path
// it visited, including the ones inside directories it was about to throw
// away.
//
// WHY THEY NEED A LONGER TIMEOUT
//
// A scan reads ~1,100 files (~9 MB). Alone, that takes well under a second.
// In the full `ng test` run, Vitest runs spec files in parallel workers, and
// the same reads have measured 3–7 s on a loaded machine, which is past
// Vitest's 5 s default. Nothing is wrong with the tree when that happens.
// The test simply ran out of time while its worker waited for the CPU.
// Making the scan cheaper shrinks that window but cannot close it, because
// the cost is the reads and every guard has to do them. So every guard
// passes SOURCE_TREE_SCAN_TIMEOUT_MS as its `it(...)` timeout. A real
// violation fails on its assertion, never on the clock, so the longer
// timeout cannot hide one.
//
// Anchor `root` on fromProjectRoot() (see project-root.ts), never on
// import.meta.url or __dirname.
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';

/**
 * Per-test timeout for a spec that scans the source tree. It is generous
 * on purpose: a timeout here only ever means "the machine was busy".
 */
export const SOURCE_TREE_SCAN_TIMEOUT_MS = 30_000;

/** Directories no guard scans: dependencies and build output. */
const ALWAYS_PRUNED_DIRS = ['node_modules', 'dist', '.angular', 'coverage'];

export interface SourceFile {
  /** Absolute path. */
  path: string;
  /** Path relative to the scan root, with platform separators. */
  relativePath: string;
  content: string;
}

export interface SourceTreeOptions {
  /** File extensions to read, e.g. `['.ts', '.html']`. Matched case-sensitively. */
  extensions: readonly string[];
  /**
   * Extra directory names to prune, e.g. `['generated']` for the
   * machine-written styles/generated output. Matched case-insensitively.
   * The walk never descends into a pruned directory.
   */
  skipDirs?: readonly string[];
  /** Return true to leave a file out, before it is read. Receives the file name. */
  skipFile?: (fileName: string) => boolean;
}

/**
 * Walk `root` once and read every file the options accept. Each file is
 * read exactly once, and only after the cheap name checks have passed.
 */
export function readSourceTree(root: string, options: SourceTreeOptions): SourceFile[] {
  const pruned = new Set([...ALWAYS_PRUNED_DIRS, ...(options.skipDirs ?? [])].map((d) => d.toLowerCase()));
  const files: SourceFile[] = [];

  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const fullPath = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!pruned.has(entry.name.toLowerCase())) walk(fullPath);
        continue;
      }
      if (!options.extensions.some((ext) => entry.name.endsWith(ext))) continue;
      if (options.skipFile?.(entry.name)) continue;
      files.push({
        path: fullPath,
        relativePath: relative(root, fullPath),
        content: readFileSync(fullPath, 'utf-8'),
      });
    }
  };

  walk(root);
  return files;
}
