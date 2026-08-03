#!/usr/bin/env node
/**
 * Pre-merge guard for DynamoDB's one-GSI-per-UpdateTable limit.
 *
 * DynamoDB's `UpdateTable` API permits exactly ONE GSI creation or deletion per
 * call. CloudFormation issues one `UpdateTable` per changed table, so a template
 * that adds two indexes to an *existing* table fails the whole deploy with:
 *
 *     Cannot perform more than one GSI creation or deletion in a single update
 *
 * ...and rolls back every other resource in the stack with it.
 *
 * The limit is specific to `UpdateTable`. `CreateTable` accepts any number of
 * indexes, so a brand-new table with five GSIs is fine — which is why this check
 * only considers tables present in BOTH the baseline and the current inventory.
 *
 * WHY THIS NEEDS A DEDICATED CHECK: an environment that deploys on every merge
 * never sees it. Two indexes landing in two PRs get two separate deploys, one
 * index each, and both succeed. Only an environment that jumps a whole release
 * at once — i.e. prod, deploying from `main` — collapses them into a single
 * `UpdateTable`. Dev soak time cannot surface this; the release diff is the only
 * place it can be caught. This took production down on 2026-08-01 (release
 * 1.12.0, two indexes on the `rag-assistants` table).
 *
 * Usage:
 *   node scripts/release/check-gsi-update-limit.mjs
 *   node scripts/release/check-gsi-update-limit.mjs --base origin/develop
 *   node scripts/release/check-gsi-update-limit.mjs --baseline-file a.json --current b.json
 *
 * Exit codes: 0 = safe to deploy in one update, 1 = must be split, 2 = bad usage.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INVENTORY_PATH = 'infrastructure/gsi-inventory.json';

function parseArgs(argv) {
  const opts = { base: 'origin/main', baselineFile: null, current: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const v = argv[i + 1];
      if (v === undefined) {
        throw new Error(`${arg} requires a value`);
      }
      i += 1;
      return v;
    };
    if (arg === '--base') opts.base = next();
    else if (arg === '--baseline-file') opts.baselineFile = next();
    else if (arg === '--current') opts.current = next();
    else if (arg === '--help' || arg === '-h') opts.help = true;
    else throw new Error(`unknown argument: ${arg}`);
  }
  return opts;
}

/** Normalise an inventory document into Map<tableName, Set<indexName>>. */
function toTableMap(doc, source) {
  const tables = doc && doc.tables;
  if (!tables || typeof tables !== 'object' || Array.isArray(tables)) {
    throw new Error(`${source}: expected an object with a "tables" property`);
  }
  const map = new Map();
  for (const [name, indexes] of Object.entries(tables)) {
    if (!Array.isArray(indexes)) {
      throw new Error(`${source}: table "${name}" must map to an array of index names`);
    }
    map.set(name, new Set(indexes));
  }
  return map;
}

/**
 * Compare two inventories and return one violation entry per table that would
 * need more than one GSI mutation in a single `UpdateTable`.
 *
 * Note the rule counts creations AND deletions together: the API limit is one
 * GSI *operation* per update, so removing one index while adding another in the
 * same deploy fails exactly like adding two does.
 */
export function findUpdateLimitViolations(baseline, current) {
  const violations = [];
  for (const [table, currentIndexes] of current) {
    const baselineIndexes = baseline.get(table);
    // Absent from the baseline => created by this deploy. `CreateTable` has no
    // per-call index limit, so any number of indexes is fine.
    if (baselineIndexes === undefined) continue;

    const added = [...currentIndexes].filter((i) => !baselineIndexes.has(i)).sort();
    const removed = [...baselineIndexes].filter((i) => !currentIndexes.has(i)).sort();
    if (added.length + removed.length > 1) {
      violations.push({ table, added, removed });
    }
  }
  // Tables dropped entirely are a `DeleteTable`, not an `UpdateTable` — no limit.
  return violations.sort((a, b) => a.table.localeCompare(b.table));
}

function readBaselineFromGit(ref) {
  try {
    return execFileSync('git', ['show', `${ref}:${INVENTORY_PATH}`], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch {
    return null;
  }
}

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(`error: ${err.message}`);
    return 2;
  }

  if (opts.help) {
    console.log(
      'Usage: check-gsi-update-limit.mjs [--base <git-ref>] ' +
        '[--baseline-file <path>] [--current <path>]',
    );
    return 0;
  }

  const currentPath = opts.current ?? resolve(REPO_ROOT, INVENTORY_PATH);
  if (!existsSync(currentPath)) {
    console.error(
      `error: ${currentPath} not found. Generate it with:\n` +
        '  cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit',
    );
    return 2;
  }

  let baselineRaw;
  let baselineLabel;
  if (opts.baselineFile) {
    if (!existsSync(opts.baselineFile)) {
      console.error(`error: baseline file ${opts.baselineFile} not found`);
      return 2;
    }
    baselineRaw = readFileSync(opts.baselineFile, 'utf8');
    baselineLabel = opts.baselineFile;
  } else {
    baselineRaw = readBaselineFromGit(opts.base);
    baselineLabel = `${opts.base}:${INVENTORY_PATH}`;
    if (baselineRaw === null) {
      // First introduction of the inventory, a shallow clone, or an unfetched
      // ref. Nothing to compare against — pass rather than block the PR.
      console.log(
        `GSI update-limit check SKIPPED — no inventory at ${baselineLabel}.\n` +
          'If this is not the commit that introduces the inventory, run ' +
          `\`git fetch origin ${opts.base.replace(/^origin\//, '')}\` and retry.`,
      );
      return 0;
    }
  }

  let baseline;
  let current;
  try {
    baseline = toTableMap(JSON.parse(baselineRaw), baselineLabel);
    current = toTableMap(JSON.parse(readFileSync(currentPath, 'utf8')), currentPath);
  } catch (err) {
    console.error(`error: ${err.message}`);
    return 2;
  }

  const violations = findUpdateLimitViolations(baseline, current);

  if (violations.length === 0) {
    console.log(
      `GSI update-limit check PASSED — ${current.size} table(s) compared against ` +
        `${baselineLabel}; no existing table needs more than one GSI operation.`,
    );
    return 0;
  }

  console.error(
    'GSI update-limit check FAILED — this deploy would need more than one GSI\n' +
      'operation on a table that already exists. DynamoDB rejects that, and\n' +
      'CloudFormation will roll back the ENTIRE stack update with it.\n',
  );
  for (const { table, added, removed } of violations) {
    console.error(`  ${table} (${added.length + removed.length} operations in one update)`);
    for (const i of added) console.error(`    + ${i}`);
    for (const i of removed) console.error(`    - ${i}`);
  }
  console.error(
    '\nSplit this into consecutive releases: ship ONE index, wait for it to\n' +
      'report ACTIVE (`aws dynamodb describe-table ... IndexStatus`), then ship\n' +
      'the next. Do the split BEFORE merging — see\n' +
      '.claude/skills/cutting-a-release/SKILL.md §1.',
  );
  return 1;
}

// Only run when invoked directly, so tests can import the pure comparison.
if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  process.exit(main());
}
