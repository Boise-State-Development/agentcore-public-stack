// project-root.ts — the one stable filesystem anchor for specs that read
// real files off disk.
//
// WHY THIS EXISTS
//
// A handful of specs are filesystem guards rather than unit tests: they read
// README.md, package.json, the committed styles/generated/*.css goldens, or
// walk src/app looking for banned CSS. They need to know where the project
// root is. The obvious anchors do not survive `--coverage`:
//
//   Without --coverage, @angular/build's unit-test builder hands each spec to
//   Vitest at its own source path, so `import.meta.url` and the `__dirname`
//   shim both point at the spec's own directory:
//
//     import.meta.url  file:///…/frontend/ai.client/src/branding/foo.spec.ts
//     __dirname        /…/frontend/ai.client/src/branding
//
//   With --coverage the builder bundles instead, emitting one flattened chunk
//   per spec AT THE PROJECT ROOT, and both anchors move with it:
//
//     import.meta.url  file:///…/frontend/ai.client/spec-branding-foo.js
//     __dirname        /…/frontend/ai.client
//
// This is an absolute relocation to the project root, not a fixed-depth
// shift — a spec nested four levels deep resolves to the project root just
// like one nested two levels deep. So every `resolve(SPEC_DIR, '../..')`
// silently changed meaning under coverage, which is why these specs passed in
// PR CI (`ng test --watch=false`) and failed every night in the coverage job.
//
// `process.cwd()` is stable ACROSS the two modes but is not pinned by the
// builder — it is whatever directory `ng` was invoked from, which may be a
// subdirectory of the project. So we walk up from it looking for
// `angular.json`, which is exactly how the Angular CLI locates the workspace
// itself; by construction this helper agrees with the tool that is running it.
//
// RULE: filesystem-reading specs must anchor on `fromProjectRoot()`. Do not
// reintroduce `import.meta.url` or `__dirname` for path resolution in a spec.
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

function findProjectRoot(): string {
  let dir = process.cwd();

  for (;;) {
    if (existsSync(resolve(dir, 'angular.json'))) {
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) {
      throw new Error(
        `Could not locate the Angular project root: no angular.json found in ${process.cwd()} ` +
          'or any parent directory. Filesystem-reading specs must be run via `ng test` from ' +
          'within frontend/ai.client.',
      );
    }
    dir = parent;
  }
}

/** Absolute path to the Angular project root (the directory holding angular.json). */
export const PROJECT_ROOT = findProjectRoot();

/** Resolve a path relative to the Angular project root, e.g. `fromProjectRoot('src', 'app')`. */
export function fromProjectRoot(...segments: string[]): string {
  return resolve(PROJECT_ROOT, ...segments);
}
