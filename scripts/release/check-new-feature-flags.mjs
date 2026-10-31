#!/usr/bin/env node
/**
 * Pre-merge guard: a release that adds a feature flag must say so.
 *
 * A new flag is a decision every deployment has to make, and the release notes
 * are the only place an operator reads before they deploy. "New or in-development
 * features default OFF" (CLAUDE.md) protects the organizations that build on this
 * stack from half-finished work switching itself on — but it also means a finished
 * feature that nobody is told about never gets turned on. The flag has to be named,
 * with its default and what setting it does, or it is a feature nobody deploys.
 *
 * WHY THIS NEEDS A DEDICATED CHECK: like the backfill gate next to this file, the
 * failure is silent. An un-set flag is not an error; it is the default. Dev cannot
 * surface it either — whoever built the feature set the variable on the
 * `development` GitHub environment the day they built it, so dev always has it.
 * Prod and every other deployment are the environments nobody told.
 *
 * WHAT IT CHECKS: every backend flag reader ADDED to
 * `backend/src/apis/shared/feature_flags.py` (an `os.environ.get("<NAME>_ENABLED"`
 * on an added line) and every SPA flag ADDED to
 * `frontend/ai.client/src/environments/feature-flags.ts` (an added
 * `<name>: boolean;` field) between the baseline and this branch must be named in
 * `RELEASE_NOTES.md`. The bar is the literal variable name (`SESSION_REWIND_ENABLED`)
 * or the literal SPA field (`sessionRewind`), because that is what an operator has
 * to type into a GitHub environment or an `environment.*.ts` file.
 *
 * WHAT IT DOES NOT CHECK: that the note says the right default, the right
 * services, or the right kind (feature switch vs rollout switch) — that is the
 * release skill's job (`.claude/skills/cutting-a-release/SKILL.md` §1c). Nor does
 * it see flags read anywhere other than those two files; the import boundary and
 * CLAUDE.md say that is where flags live.
 *
 * Usage:
 *   node scripts/release/check-new-feature-flags.mjs
 *   node scripts/release/check-new-feature-flags.mjs --base origin/develop
 *   node scripts/release/check-new-feature-flags.mjs --added A_ENABLED,features.b --notes-file n.md
 *
 * Exit codes: 0 = nothing to announce, or all announced; 1 = unannounced flag;
 * 2 = bad usage.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const NOTES_PATH = 'RELEASE_NOTES.md';
const BACKEND_FLAGS = 'backend/src/apis/shared/feature_flags.py';
const SPA_FLAGS = 'frontend/ai.client/src/environments/feature-flags.ts';

// An added line in feature_flags.py that reads a flag variable.
const BACKEND_RE = /^\+.*os\.environ\.get\(\s*["']([A-Z0-9_]+_ENABLED)["']/;
// An added field on the FeatureFlags interface. Comment lines never match
// because they do not end in `: boolean;`.
const SPA_RE = /^\+\s+([a-zA-Z0-9_]+)\s*:\s*boolean\s*;/;

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

/** Flags added on this branch relative to `base`, as `NAME_ENABLED` / `features.name`. */
function addedFlags(base) {
  let out;
  try {
    out = execFileSync(
      'git',
      ['diff', '--unified=0', `${base}...HEAD`, '--', BACKEND_FLAGS, SPA_FLAGS],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
  } catch {
    // No baseline to diff against (a fresh clone, or `main` predates this check).
    return null;
  }
  const found = new Set();
  for (const line of out.split('\n')) {
    if (line.startsWith('+++')) continue;
    const b = BACKEND_RE.exec(line);
    if (b) found.add(b[1]);
    const s = SPA_RE.exec(line);
    if (s) found.add(`features.${s[1]}`);
  }
  return [...found].sort();
}

function readNotes(notesFile) {
  const path = notesFile ? resolve(notesFile) : resolve(REPO_ROOT, NOTES_PATH);
  if (!existsSync(path)) return null;
  return readFileSync(path, 'utf8');
}

/** What the notes must contain for one flag: the bare name an operator types. */
function needle(flag) {
  return flag.startsWith('features.') ? flag.slice('features.'.length) : flag;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));

  const added =
    opts.added !== null
      ? [...new Set(opts.added.split(',').map((s) => s.trim()).filter(Boolean))].sort()
      : addedFlags(opts.base);

  if (added === null) {
    console.log(`SKIPPED — no baseline at ${opts.base} to diff against.`);
    console.log('Check by hand: does this release add a feature flag?');
    process.exit(0);
  }

  if (added.length === 0) {
    console.log('No feature flags added in this range — nothing to announce.');
    process.exit(0);
  }

  const notes = readNotes(opts.notesFile);
  if (notes === null) {
    console.error(`error: ${NOTES_PATH} not found`);
    process.exit(2);
  }

  const missing = added.filter((f) => !notes.includes(needle(f)));

  console.log(`Feature flags added since ${opts.base}: ${added.length}`);
  for (const f of added) {
    console.log(`  ${missing.includes(f) ? '✗' : '✓'} ${f}`);
  }

  if (missing.length === 0) {
    console.log(`\nAll named in ${NOTES_PATH}.`);
    process.exit(0);
  }

  console.error(`\n${missing.length} feature flag(s) are not named in ${NOTES_PATH}:`);
  for (const f of missing) console.error(`  ${f}`);
  console.error(
    '\nAdd each one to the release notes with its default, the services it is set on, ' +
      'the CDK_* variable and SPA field that set it, and whether it is a feature switch ' +
      'or a rollout switch. An unannounced flag is a feature nobody turns on.',
  );
  process.exit(1);
}

main();
