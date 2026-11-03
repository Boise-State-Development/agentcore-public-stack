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
 * WHAT ELSE IT CHECKS — flags on in prod but off by default. The other way a
 * feature goes missing is slower: we turn a finished feature on in our own
 * `production` environment, mean to promote it to default-on "later", and forget.
 * Nothing goes red; a fresh install simply lacks the feature. So every SPA flag
 * that is `true` in `environment.production.ts` and `false` in `environment.ts`
 * (the local file, which carries the code default) must be named in the NEWEST
 * `RELEASE_NOTES.md` entry — each release has to say "promoted" or why it is
 * still opt-in. The list of such flags, with each one's promotion condition, is
 * tracked in issue #1798. Backend-only flags have no SPA field and are listed on
 * that issue by hand; this check cannot see them.
 *
 * WHAT IT DOES NOT CHECK: that the note says the right default, the right
 * services, or the right kind (feature switch vs rollout switch) — that is the
 * release skill's job (`.claude/skills/cutting-a-release/SKILL.md` §1c). Nor does
 * it see flags read anywhere other than those files; the import boundary and
 * CLAUDE.md say that is where flags live.
 *
 * Usage:
 *   node scripts/release/check-new-feature-flags.mjs
 *   node scripts/release/check-new-feature-flags.mjs --base origin/develop
 *   node scripts/release/check-new-feature-flags.mjs --added A_ENABLED,features.b --notes-file n.md
 *   node scripts/release/check-new-feature-flags.mjs --added '' --prod-only features.x --notes-file n.md
 *
 * Exit codes: 0 = nothing to announce, or all announced; 1 = an unannounced new
 * flag, or a prod-on/default-off flag the newest notes entry does not name;
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
const SPA_ENV_LOCAL = 'frontend/ai.client/src/environments/environment.ts';
const SPA_ENV_PROD = 'frontend/ai.client/src/environments/environment.production.ts';
const PROMOTION_ISSUE = '#1798';

// An added line in feature_flags.py that reads a flag variable.
const BACKEND_RE = /^\+.*os\.environ\.get\(\s*["']([A-Z0-9_]+_ENABLED)["']/;
// An added field on the FeatureFlags interface. Comment lines never match
// because they do not end in `: boolean;`.
const SPA_RE = /^\+\s+([a-zA-Z0-9_]+)\s*:\s*boolean\s*;/;
// A flag value inside an environment file's `features: { ... }` block.
const SPA_VALUE_RE = /^\s+([a-zA-Z0-9_]+)\s*:\s*(true|false)\s*,/gm;
// The heading that opens each release-notes entry; the newest entry is the text
// between the first two.
const NOTES_ENTRY_RE = /^# Release Notes — v/m;

function parseArgs(argv) {
  const opts = { base: 'origin/main', added: null, notesFile: null, prodOnly: null };
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
    else if (arg === '--prod-only') opts.prodOnly = next();
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

/** `{ name: boolean }` for the `features` block of one SPA environment file. */
function spaFlagValues(relPath) {
  const path = resolve(REPO_ROOT, relPath);
  if (!existsSync(path)) return null;
  const src = readFileSync(path, 'utf8');
  const values = {};
  for (const m of src.matchAll(SPA_VALUE_RE)) values[m[1]] = m[2] === 'true';
  return values;
}

/**
 * SPA flags on in prod but off by default — `true` in environment.production.ts
 * and `false` in environment.ts — as `features.<name>`. These are the features a
 * fresh install does not get until someone promotes them, so each release must
 * account for them. `null` when either file is missing.
 */
function prodOnlyFlags() {
  const local = spaFlagValues(SPA_ENV_LOCAL);
  const prod = spaFlagValues(SPA_ENV_PROD);
  if (local === null || prod === null) return null;
  return Object.keys(prod)
    .filter((name) => prod[name] === true && local[name] === false)
    .sort()
    .map((name) => `features.${name}`);
}

/** The newest entry of the notes: from its heading to the next one. */
function newestEntry(notes) {
  const headings = [...notes.matchAll(new RegExp(NOTES_ENTRY_RE.source, 'gm'))];
  if (headings.length === 0) return notes;
  const start = headings[0].index;
  const end = headings.length > 1 ? headings[1].index : notes.length;
  return notes.slice(start, end);
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

  const prodOnly =
    opts.prodOnly !== null
      ? [...new Set(opts.prodOnly.split(',').map((s) => s.trim()).filter(Boolean))].sort()
      : prodOnlyFlags();

  if (added === null) {
    console.log(`SKIPPED — no baseline at ${opts.base} to diff against.`);
    console.log('Check by hand: does this release add a feature flag?');
  } else if (added.length === 0) {
    console.log('No feature flags added in this range — nothing to announce.');
  }

  const needsNotes = (added !== null && added.length > 0) || (prodOnly !== null && prodOnly.length > 0);
  if (!needsNotes) {
    if (prodOnly !== null) console.log('No SPA flags are on in prod but off by default.');
    process.exit(0);
  }

  const notes = readNotes(opts.notesFile);
  if (notes === null) {
    console.error(`error: ${NOTES_PATH} not found`);
    process.exit(2);
  }
  const newest = newestEntry(notes);

  let failed = false;

  if (added !== null && added.length > 0) {
    const missing = added.filter((f) => !notes.includes(needle(f)));
    console.log(`Feature flags added since ${opts.base}: ${added.length}`);
    for (const f of added) console.log(`  ${missing.includes(f) ? '✗' : '✓'} ${f}`);
    if (missing.length === 0) {
      console.log(`All named in ${NOTES_PATH}.`);
    } else {
      failed = true;
      console.error(`\n${missing.length} feature flag(s) are not named in ${NOTES_PATH}:`);
      for (const f of missing) console.error(`  ${f}`);
      console.error(
        '\nAdd each one to the release notes with its default, the services it is set on, ' +
          'the CDK_* variable and SPA field that set it, and whether it is a feature switch ' +
          'or a rollout switch. An unannounced flag is a feature nobody turns on.',
      );
    }
  }

  if (prodOnly !== null && prodOnly.length > 0) {
    const unaccounted = prodOnly.filter((f) => !newest.includes(needle(f)));
    console.log(`\nSPA flags on in prod but off by default (promotion candidates): ${prodOnly.length}`);
    for (const f of prodOnly) console.log(`  ${unaccounted.includes(f) ? '✗' : '✓'} ${f}`);
    if (unaccounted.length === 0) {
      console.log(`All accounted for in the newest ${NOTES_PATH} entry.`);
    } else {
      failed = true;
      console.error(
        `\n${unaccounted.length} flag(s) are on in prod but off by default, and the newest ` +
          `${NOTES_PATH} entry does not mention them:`,
      );
      for (const f of unaccounted) console.error(`  ${f}`);
      console.error(
        `\nEach release must say what happens to these: promote it to default-on in this ` +
          `release, or name it under "Still opt-in" with the reason it stays off by default. ` +
          `A flag that is on for us and off by default is a feature a fresh install never ` +
          `gets, and nothing goes red to say so. The list and each flag's promotion ` +
          `condition live in ${PROMOTION_ISSUE}.`,
      );
    }
  }

  process.exit(failed ? 1 : 0);
}

main();
