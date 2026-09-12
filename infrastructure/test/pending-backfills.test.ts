/**
 * Exit-code contract for `scripts/release/check-pending-backfills.mjs`.
 *
 * The gate itself runs on PRs into `main` (`.github/workflows/pending-backfills.yml`)
 * and requires every backfill script ADDED in a release to be named in
 * RELEASE_NOTES.md — because a backfill is not code that runs itself, and an
 * unrun one fails silently as a short list rather than an error.
 *
 * Lives here, next to `gsi-update-limit.test.ts`, because this is the other half
 * of the same idea: a release-only hazard that no amount of dev soak time can
 * surface, guarded by a script whose exit codes CI depends on.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const REPO_ROOT = resolve(__dirname, '..', '..');
const CHECKER = resolve(REPO_ROOT, 'scripts/release/check-pending-backfills.mjs');

describe('check-pending-backfills.mjs', () => {
  let dir: string;

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'backfill-check-'));
  });

  /** Run the real CLI the way CI does; returns its exit code and output. */
  function run(added: string[], notes: string) {
    const notesPath = join(dir, `notes-${Math.random().toString(36).slice(2)}.md`);
    writeFileSync(notesPath, notes);

    try {
      const stdout = execFileSync(
        process.execPath,
        [CHECKER, '--added', added.join(','), '--notes-file', notesPath],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      );
      return { code: 0, output: stdout };
    } catch (err) {
      const e = err as { status: number; stdout: string; stderr: string };
      return { code: e.status, output: `${e.stdout}${e.stderr}` };
    }
  }

  it('passes when the release adds no backfill', () => {
    const { code } = run([], '# 1.2.3\n\nNothing to see.\n');
    expect(code).toBe(0);
  });

  it('fails when a backfill ships unannounced — the case this exists for', () => {
    const { code, output } = run(
      ['backend/scripts/backfill_tool_catalog_index.py'],
      '# 1.2.3\n\nAdded a nice feature.\n',
    );
    expect(code).toBe(1);
    expect(output).toContain('backfill_tool_catalog_index.py');
  });

  it('passes when the notes name the script', () => {
    const { code } = run(
      ['backend/scripts/backfill_tool_catalog_index.py'],
      '# 1.2.3\n\nRun `backfill_tool_catalog_index.py` against each environment.\n',
    );
    expect(code).toBe(0);
  });

  it('accepts the full path spelling too', () => {
    const { code } = run(
      ['backend/scripts/backfill_tool_catalog_index.py'],
      '# 1.2.3\n\nRun backend/scripts/backfill_tool_catalog_index.py --apply\n',
    );
    expect(code).toBe(0);
  });

  it('fails the release when only SOME of several are named', () => {
    // The dangerous near-miss: the notes mention a backfill, so a human skims
    // past, but a second one is missing.
    const { code, output } = run(
      [
        'backend/scripts/backfill_tool_catalog_index.py',
        'backend/scripts/backfill_widget_owner_keys.py',
      ],
      '# 1.2.3\n\nRun `backfill_tool_catalog_index.py`.\n',
    );
    expect(code).toBe(1);
    expect(output).toContain('backfill_widget_owner_keys.py');
    expect(output).not.toMatch(/✗ .*backfill_tool_catalog_index/);
  });

  it('ignores non-backfill scripts in the same directory', () => {
    const { code } = run(
      ['backend/scripts/seed_bootstrap_data.py'],
      '# 1.2.3\n\nNo mention.\n',
    );
    expect(code).toBe(0);
  });
});
