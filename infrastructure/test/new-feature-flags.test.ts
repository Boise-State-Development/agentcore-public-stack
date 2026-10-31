/**
 * Exit-code contract for `scripts/release/check-new-feature-flags.mjs`.
 *
 * The gate itself runs on PRs into `main` (`.github/workflows/new-feature-flags.yml`)
 * and requires every feature flag ADDED in a release — a backend `*_ENABLED`
 * reader in `feature_flags.py`, an SPA field on `FeatureFlags` — to be named in
 * RELEASE_NOTES.md. New features default off so that half-finished work never
 * switches itself on for a deployment; the other half of that rule is that a
 * finished feature nobody is told about never gets turned on. It also requires
 * every SPA flag that is on in prod but off by default to be accounted for in
 * the newest notes entry, so "promote it later" cannot be forgotten (#1798).
 *
 * Lives here, next to `pending-backfills.test.ts`, because it is the same idea:
 * a release-only hazard that dev soak time cannot surface (whoever built the
 * feature set the variable on dev the day they built it), guarded by a script
 * whose exit codes CI depends on.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const REPO_ROOT = resolve(__dirname, '..', '..');
const CHECKER = resolve(REPO_ROOT, 'scripts/release/check-new-feature-flags.mjs');

describe('check-new-feature-flags.mjs', () => {
  let dir: string;

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'flag-check-'));
  });

  /**
   * Run the real CLI the way CI does; returns its exit code and output.
   * `prodOnly` stands in for the environment-file comparison; it defaults to
   * none so the "new flag" cases are not coloured by this branch's real
   * promotion candidates.
   */
  function run(added: string[], notes: string, prodOnly: string[] = []) {
    const notesPath = join(dir, `notes-${Math.random().toString(36).slice(2)}.md`);
    writeFileSync(notesPath, notes);

    try {
      const stdout = execFileSync(
        process.execPath,
        [
          CHECKER,
          '--added', added.join(','),
          '--prod-only', prodOnly.join(','),
          '--notes-file', notesPath,
        ],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      );
      return { code: 0, output: stdout };
    } catch (err) {
      const e = err as { status: number; stdout: string; stderr: string };
      return { code: e.status, output: `${e.stdout}${e.stderr}` };
    }
  }

  it('passes when the release adds no flag', () => {
    const { code } = run([], '# 1.2.3\n\nNothing to see.\n');
    expect(code).toBe(0);
  });

  it('fails when a backend flag ships unannounced — the case this exists for', () => {
    const { code, output } = run(['SESSION_REWIND_ENABLED'], '# 1.2.3\n\nAdded rewind.\n');
    expect(code).toBe(1);
    expect(output).toContain('SESSION_REWIND_ENABLED');
  });

  it('passes when the notes name the backend variable', () => {
    const { code } = run(
      ['SESSION_REWIND_ENABLED'],
      '# 1.2.3\n\nOff by default: set `SESSION_REWIND_ENABLED` / `CDK_SESSION_REWIND_ENABLED`.\n',
    );
    expect(code).toBe(0);
  });

  it('accepts the SPA field named bare or as features.<name>', () => {
    expect(run(['features.sessionRewind'], '# 1.2.3\n\nSPA: `features.sessionRewind`.\n').code).toBe(0);
    expect(run(['features.sessionRewind'], '# 1.2.3\n\nSet `sessionRewind` to true.\n').code).toBe(0);
  });

  it('fails the release when only SOME of several are named', () => {
    // The dangerous near-miss: the notes name one side of a two-sided flag, so a
    // human skims past, and the SPA half is never set.
    const { code, output } = run(
      ['SESSION_REWIND_ENABLED', 'features.sessionRewind'],
      '# 1.2.3\n\nSet `SESSION_REWIND_ENABLED`.\n',
    );
    expect(code).toBe(1);
    expect(output).toContain('features.sessionRewind');
    expect(output).not.toMatch(/✗ SESSION_REWIND_ENABLED/);
  });

  describe('flags on in prod but off by default', () => {
    const TWO_ENTRIES =
      '# Release Notes — v1.33.0\n\nNewest.\n\n# Release Notes — v1.32.0\n\nStill opt-in: `sessionRewind`.\n';

    it('fails when a promotion candidate is named only in an OLDER entry — the forgetting case', () => {
      // The release that turned it on named it. The next release did not. That
      // is exactly the release in which "promote it later" gets lost.
      const { code, output } = run([], TWO_ENTRIES, ['features.sessionRewind']);
      expect(code).toBe(1);
      expect(output).toContain('features.sessionRewind');
      expect(output).toContain('#1798');
    });

    it('passes when the newest entry accounts for it', () => {
      const notes =
        '# Release Notes — v1.33.0\n\nStill opt-in: `sessionRewind` (promote after two weeks in prod).\n\n' +
        '# Release Notes — v1.32.0\n\nOlder.\n';
      expect(run([], notes, ['features.sessionRewind']).code).toBe(0);
    });

    it('treats a notes file with no entry headings as one entry', () => {
      expect(run([], '# 1.2.3\n\nStill opt-in: `sessionRewind`.\n', ['features.sessionRewind']).code).toBe(0);
    });

    it('reports both failures at once when a new flag and a candidate are both missing', () => {
      const { code, output } = run(['NEW_THING_ENABLED'], '# Release Notes — v1.33.0\n\nNothing.\n', [
        'features.sessionFork',
      ]);
      expect(code).toBe(1);
      expect(output).toContain('NEW_THING_ENABLED');
      expect(output).toContain('features.sessionFork');
    });
  });

  it('matches the live diff against this branch without crashing', () => {
    // Exercises the git path. We cannot assert on the result (it depends on what
    // this branch adds), only that the script runs and reports one of its
    // three states.
    let output: string;
    try {
      output = execFileSync(process.execPath, [CHECKER, '--base', 'HEAD'], {
        cwd: REPO_ROOT,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      const e = err as { stdout: string; stderr: string };
      output = `${e.stdout}${e.stderr}`;
    }
    expect(output).toMatch(/No feature flags added|Feature flags added since|SKIPPED/);
    // The live environment files are compared too; this branch's candidates are
    // all accounted for in the newest RELEASE_NOTES.md entry, or the gate fails.
    expect(output).toMatch(/promotion candidates|No SPA flags are on in prod/);
  });
});
