// start-script-generator-parity.spec.ts
//
// Property 6: start.ps1 runs whatever `prestart` declares, without
// duplicating the list. There is no Pester harness anywhere in this repo
// (four `.ps1` files, none of them tests), so start.ps1 cannot be
// unit-tested directly. What vitest CAN check is the invariant that
// matters and is exactly the shape of drift this bug was: start.ps1
// contains no `generate-*.ts` literal, and it does reference `prestart`.
//
// Reading a file outside the Angular project root (../../../../start.ps1)
// is new for this repo — rebranding-guide.doc-presence.spec.ts reads
// ./README.md inside src/branding/ — but node:fs does not care, and both
// local runs and CI checkouts have the repo root present.
//
// See design.md "Correctness Properties" (Property 6) and bugfix.md
// 2.1, 2.2, 3.6, 3.7.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));
const START_PS1_PATH = resolve(SPEC_DIR, '../../../../start.ps1');
const PACKAGE_JSON_PATH = resolve(SPEC_DIR, '../../package.json');

describe('start.ps1 / package.json prestart single-source-of-truth (Property 6)', () => {
  it('start.ps1 exists and is readable from the repo root', () => {
    expect(() => readFileSync(START_PS1_PATH, 'utf8')).not.toThrow();
  });

  it('contains no generate-*.ts literal', () => {
    const contents = readFileSync(START_PS1_PATH, 'utf8');
    expect(contents).not.toMatch(/generate-[a-zA-Z-]+\.ts/);
  });

  it('references prestart, so the generator list is sourced from package.json rather than restated', () => {
    const contents = readFileSync(START_PS1_PATH, 'utf8');
    expect(contents).toContain('prestart');
  });

  it('reports each prestart segment individually rather than a single all-or-nothing invocation (contains a split on &&)', () => {
    const contents = readFileSync(START_PS1_PATH, 'utf8');
    // The segment-splitting loop divides prestart's chain on '&&' so each
    // generator's success/failure is reported and one failure does not
    // hide the remaining generators (Requirement 2.2, 3.7).
    expect(contents).toMatch(/-split\s+'&&'/);
  });

  it("package.json's prestart script exists and chains at least the three surface-relevant generators", () => {
    const pkg = JSON.parse(readFileSync(PACKAGE_JSON_PATH, 'utf8')) as { scripts?: Record<string, string> };
    const prestart = pkg.scripts?.['prestart'];
    expect(typeof prestart).toBe('string');
    expect(prestart).toContain('generate-brand-theme.ts');
    expect(prestart).toContain('generate-surface-theme.ts');
    expect(prestart).toContain('generate-surface-colors.ts');
  });

  it('adding a new generator to prestart requires no start.ps1 edit (drift-detection invariant, not a literal-name check)', () => {
    // This is the invariant itself, restated as a single assertion: as
    // long as start.ps1 never names a generator, whatever prestart lists
    // is what runs. There is nothing further to assert here beyond the
    // "no literal" check above — this test documents *why* that check is
    // the right one, so a future reader does not try to "fix" it by
    // asserting the current generator count (which would recreate a
    // hand-maintained mirror in the test suite instead of the script).
    const contents = readFileSync(START_PS1_PATH, 'utf8');
    expect(contents).not.toMatch(/generate-[a-zA-Z-]+\.ts/);
  });
});
