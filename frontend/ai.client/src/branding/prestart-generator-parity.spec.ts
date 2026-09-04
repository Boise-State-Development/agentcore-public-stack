// prestart-generator-parity.spec.ts
//
// Single-source-of-truth guard for the branding generators. The failure
// this protects against is the one that motivated the (now deleted)
// start.ps1 parity test: a generator script exists on disk but a launcher
// does not run it, so the dev server / build ships stale generated assets.
//
// With the local-only start.ps1 out of the picture, the coupling that
// remains lives entirely in committed files — the generator scripts in
// scripts/branding/ and the `prestart` / `prebuild` npm lifecycle hooks in
// package.json. `prestart` runs before `npm start` (local dev); `prebuild`
// runs before `npm run build` (CI/deploy). Both must run every generator.
//
// The generator list is DISCOVERED by globbing scripts/branding/ rather
// than hardcoded here, so adding a generator needs no edit to this test —
// it just has to be wired into both hooks. That is deliberately the
// opposite of a hand-maintained mirror: the test fails only when a real
// generator is missing from a hook, not when the count changes.
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));
const PACKAGE_JSON_PATH = resolve(SPEC_DIR, '../../package.json');
const GENERATORS_DIR = resolve(SPEC_DIR, '../../scripts/branding');

// Every generator is named generate-*.ts. color-math.ts and any other
// helper module is intentionally excluded: only entry-point generators
// belong in a lifecycle hook.
const generatorFiles = readdirSync(GENERATORS_DIR).filter(
  (f) => /^generate-.*\.ts$/.test(f),
);

const pkg = JSON.parse(readFileSync(PACKAGE_JSON_PATH, 'utf8')) as {
  scripts?: Record<string, string>;
};
const prestart = pkg.scripts?.['prestart'] ?? '';
const prebuild = pkg.scripts?.['prebuild'] ?? '';

describe('branding generator / npm lifecycle parity', () => {
  it('discovers at least one generator to guard', () => {
    // A sanity check: if the glob ever returns nothing (dir moved/renamed),
    // the per-generator assertions below would vacuously pass, so fail loud.
    expect(generatorFiles.length).toBeGreaterThan(0);
  });

  it('defines both prestart and prebuild scripts', () => {
    expect(prestart).not.toBe('');
    expect(prebuild).not.toBe('');
  });

  it.each(generatorFiles)('prestart runs %s', (file) => {
    expect(prestart).toContain(file);
  });

  it.each(generatorFiles)('prebuild runs %s', (file) => {
    expect(prebuild).toContain(file);
  });

  it('prestart and prebuild run the same set of generators', () => {
    const inPrestart = generatorFiles.filter((f) => prestart.includes(f)).sort();
    const inPrebuild = generatorFiles.filter((f) => prebuild.includes(f)).sort();
    expect(inPrestart).toEqual(inPrebuild);
  });
});
