#!/usr/bin/env node
/**
 * Pre-merge guard: a release that ships a data backfill must say so.
 *
 * A backfill script is not code that runs itself. Someone has to run it, against
 * each environment, in a window that matters — and the release notes are the only
 * place an operator looks for that. A backfill that ships unannounced is a
 * migration nobody performs.
 *
 * WHY THIS NEEDS A DEDICATED CHECK: the failure is silent in exactly the same
 * shape as the GSI limit this file sits next to. Backfills exist to populate a
 * sparse index or a new attribute, and the code that reads it does not error when
 * the data is missing — a sparse index returns *fewer rows*, not an exception. So
 * an unrun backfill looks like a short list, not an outage: the tool catalog is
 * missing half its tools, the library is missing the older artifacts. Nothing goes
 * red. And `develop` cannot surface it either, because whoever wrote the script
 * ran it against dev by hand the day they wrote it; prod is the environment with
 * nobody assigned.
 *
 * WHAT IT CHECKS: every `backend/scripts/backfill_*.py` ADDED between the baseline
 * and this branch must be named in `RELEASE_NOTES.md`. Naming the file is the bar
 * because that is what an operator needs to type — a prose mention of "a backfill
 * is required" that omits the script name is exactly the release note that sends
 * someone digging through git log.
 *
 * WHAT IT DOES NOT CHECK: that the backfill was actually *run*. No CI job can know
 * that. This guarantees the instruction reaches the person who can.
 *
 * Usage:
 *   node scripts/release/check-pending-backfills.mjs
 *   node scripts/release/check-pending-backfills.mjs --base origin/develop
 *   node scripts/release/check-pending-backfills.mjs --added a.py,b.py --notes-file n.md
 *
 * Exit codes: 0 = nothing to announce, or all announced; 1 = unannounced backfill;
 * 2 = bad usage.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { basename, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const NOTES_PATH = 'RELEASE_NOTES.md';
const BACKFILL_DIR = 'backend/scripts';
const BACKFILL_RE = /^backend\/scripts\/backfill_[^/]+\.py$/;

function parseArgs(argv) {
  const opts = { base: 'origin/main', added: null, notesFile: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const v = argv[i + 1];
      if (v === undefined) {
        console.error(`error: ${arg} needs a value`);
        process.exit(2);
      }
      i += 1;
      return v;
    };
    if (arg === '--base') opts.base = next();
    else if (arg === '--added') opts.added = next();
    else if (arg === '--notes-file') opts.notesFile = next();
    else if (arg === '--help' || arg === '-h') {
      console.log(readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0]);
      process.exit(0);
    } else {
      console.error(`error: unknown argument ${arg}`);
      process.exit(2);
    }
  }
  return opts;
}

/** Backfill scripts added on this branch relative to `base`. */
function addedBackfills(base) {
  let out;
  try {
    out = execFileSync(
      'git',
      ['diff', '--name-only', '--diff-filter=A', `${base}...HEAD`, '--', BACKFILL_DIR],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
  } catch {
    // No baseline to diff against (a fresh clone, or `main` predates this check).
    // Reporting SKIPPED beats failing every release until the first one lands.
    return null;
  }
  return out.split('\n').map((l) => l.trim()).filter((l) => BACKFILL_RE.test(l));
}

function readNotes(notesFile) {
  const path = notesFile ? resolve(notesFile) : resolve(REPO_ROOT, NOTES_PATH);
  if (!existsSync(path)) return null;
  return readFileSync(path, 'utf8');
}

function main() {
  const opts = parseArgs(process.argv.slice(2));

  // Both paths apply the same filename filter, so `--added` (tests, local runs)
  // cannot classify a file differently from the git path CI uses.
  const added =
    opts.added !== null
      ? opts.added
          .split(',')
          .map((s) => s.trim())
          .filter((s) => BACKFILL_RE.test(s))
      : addedBackfills(opts.base);

  if (added === null) {
    console.log(`SKIPPED — no baseline at ${opts.base} to diff against.`);
    console.log('Check by hand: does this release add a backfill script?');
    process.exit(0);
  }

  if (added.length === 0) {
    console.log('No backfill scripts added in this range — nothing to announce.');
    process.exit(0);
  }

  const notes = readNotes(opts.notesFile);
  if (notes === null) {
    console.error(`error: ${NOTES_PATH} not found`);
    process.exit(2);
  }

  // Match on the bare filename rather than the full path: release notes reasonably
  // write `backfill_tool_catalog_index.py` or the full `backend/scripts/...` form,
  // and both name the thing an operator has to run.
  const missing = added.filter((p) => !notes.includes(basename(p)));

  console.log(`Backfill scripts added since ${opts.base}: ${added.length}`);
  for (const p of added) {
    console.log(`  ${missing.includes(p) ? '✗' : '✓'} ${p}`);
  }

  if (missing.length === 0) {
    console.log(`\nAll named in ${NOTES_PATH}.`);
    process.exit(0);
  }

  console.error(
    `\n${missing.length} backfill script(s) are not named in ${NOTES_PATH}:`,
  );
  for (const p of missing) console.error(`  ${p}`);
  console.error(
    '\nAdd each one to the release notes with the command to run it and the ' +
      'environments it must run against. An unannounced backfill is a migration ' +
      'nobody performs — and it fails silently, as a short list rather than an error.',
  );
  process.exit(1);
}

main();
