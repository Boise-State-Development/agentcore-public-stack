/**
 * DynamoDB GSI inventory + the one-GSI-per-UpdateTable guard.
 *
 * DynamoDB's `UpdateTable` API permits exactly ONE GSI creation or deletion per
 * call. CloudFormation issues one `UpdateTable` per changed table, so a template
 * that adds two indexes to an *existing* table fails the deploy and rolls the
 * whole stack back with it. `CreateTable` has no such limit, so a brand-new
 * table with any number of indexes is fine.
 *
 * An environment that deploys on every merge never sees this: two indexes from
 * two PRs get two deploys, one index each. Only an environment that jumps a
 * whole release at once — prod, deploying from `main` — collapses them into a
 * single update. That is how release 1.12.0 took production down on 2026-08-01.
 *
 * This file owns the *generation* half of the guard: it synthesizes the app and
 * keeps `infrastructure/gsi-inventory.json` honest, so the committed inventory
 * can never silently drift from the CDK code. The *comparison* half — diffing
 * the inventory against `origin/main` — lives in
 * `scripts/release/check-gsi-update-limit.mjs` and runs in CI on PRs into
 * `main`. Its exit-code contract is covered at the bottom of this file.
 *
 * Regenerate after intentionally adding or removing an index:
 *
 *     cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  createMockConfig,
  mockSsmContext,
  MOCK_ACCOUNT,
  MOCK_PREFIX,
  MOCK_REGION,
} from './helpers/mock-config';

const REPO_ROOT = resolve(__dirname, '..', '..');
const INVENTORY_PATH = resolve(REPO_ROOT, 'infrastructure/gsi-inventory.json');
const CHECKER = resolve(REPO_ROOT, 'scripts/release/check-gsi-update-limit.mjs');

interface Inventory {
  tables: Record<string, string[]>;
}

/**
 * Synthesize the whole app and inventory every DynamoDB table's indexes.
 *
 * Keyed by physical table name with the project prefix stripped, because the
 * per-update limit applies to the physical table — that is the identity that
 * survives a construct-tree refactor and the identity CloudFormation issues the
 * `UpdateTable` against.
 */
function buildInventory(): Inventory {
  const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
  const config = createMockConfig({
    domainName: 'example.com',
    infrastructureHostedZoneDomain: 'example.com',
    certificateArn: cert,
    frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
    artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
    mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
    fineTuning: {},
  });

  const app = new cdk.App();
  mockSsmContext(app, config);
  const platform = new PlatformStack(app, 'Platform', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  platform.wireCompute();

  const resources = Template.fromStack(platform).findResources('AWS::DynamoDB::Table');

  const tables: Record<string, string[]> = {};
  for (const [logicalId, resource] of Object.entries(resources)) {
    const props = (resource as { Properties?: Record<string, unknown> }).Properties ?? {};
    const tableName = props.TableName;
    // Fall back to the logical id for any table CDK auto-names — still a stable
    // key, and better than dropping the table from the inventory entirely.
    const key =
      typeof tableName === 'string'
        ? tableName.replace(new RegExp(`^${MOCK_PREFIX}-`), '')
        : `<logical:${logicalId}>`;

    const gsis = (props.GlobalSecondaryIndexes ?? []) as Array<{ IndexName?: unknown }>;
    const indexNames = gsis
      .map((g) => g.IndexName)
      .filter((n): n is string => typeof n === 'string')
      .sort();

    if (key in tables) {
      throw new Error(`Duplicate table key "${key}" in inventory (logical id ${logicalId})`);
    }
    tables[key] = indexNames;
  }

  // Sort keys so the committed file diffs cleanly and reviewers can see an added
  // index as a one-line change.
  const sorted: Record<string, string[]> = {};
  for (const key of Object.keys(tables).sort()) sorted[key] = tables[key];
  return { tables: sorted };
}

function serialize(inventory: Inventory): string {
  return `${JSON.stringify(inventory, null, 2)}\n`;
}

describe('DynamoDB GSI inventory', () => {
  let inventory: Inventory;

  beforeAll(() => {
    inventory = buildInventory();
    if (process.env.UPDATE_GSI_INVENTORY) {
      writeFileSync(INVENTORY_PATH, serialize(inventory));
    }
  });

  it('matches the committed infrastructure/gsi-inventory.json', () => {
    const committed = readFileSync(INVENTORY_PATH, 'utf8');
    expect(serialize(inventory)).toEqual(committed);
    // If this fails you added or removed a GSI. Regenerate with:
    //   cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit
    // then check the diff: if an EXISTING table gained more than one index, the
    // release must be split across two deploys. See
    // .claude/skills/cutting-a-release/SKILL.md §1.
  });

  it('captures every table the stack creates', () => {
    // Guards against a synth-shape change silently emptying the inventory,
    // which would make the checker vacuously pass forever.
    expect(Object.keys(inventory.tables).length).toBeGreaterThan(15);
  });

  it('records the assistants-table indexes that caused the 1.12.0 outage', () => {
    // Regression anchor: these two arrived in separate develop merges and
    // collapsed into one prod UpdateTable.
    expect(inventory.tables['rag-assistants']).toEqual(
      expect.arrayContaining(['AgentDirectoryIndex', 'AgentReportsIndex']),
    );
  });

  it('names no table by logical-id fallback', () => {
    // Every table should have an explicit TableName; a fallback key means the
    // inventory key would churn on an unrelated construct-tree refactor.
    const fallbacks = Object.keys(inventory.tables).filter((k) => k.startsWith('<logical:'));
    expect(fallbacks).toEqual([]);
  });
});

describe('check-gsi-update-limit.mjs', () => {
  let dir: string;

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'gsi-check-'));
  });

  /** Run the real CLI the way CI does; returns its exit code and output. */
  function run(baseline: Inventory, current: Inventory) {
    const basePath = join(dir, `base-${Math.random().toString(36).slice(2)}.json`);
    const currPath = join(dir, `curr-${Math.random().toString(36).slice(2)}.json`);
    writeFileSync(basePath, serialize(baseline));
    writeFileSync(currPath, serialize(current));

    try {
      const stdout = execFileSync(
        process.execPath,
        [CHECKER, '--baseline-file', basePath, '--current', currPath],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      );
      return { code: 0, output: stdout };
    } catch (err) {
      const e = err as { status: number; stdout: string; stderr: string };
      return { code: e.status, output: `${e.stdout}${e.stderr}` };
    }
  }

  it('passes when an existing table gains exactly one index', () => {
    const { code } = run({ tables: { t: ['A'] } }, { tables: { t: ['A', 'B'] } });
    expect(code).toBe(0);
  });

  it('fails when an existing table gains two indexes — the 1.12.0 shape', () => {
    const { code, output } = run(
      { tables: { 'rag-assistants': ['OwnerStatusIndex'] } },
      { tables: { 'rag-assistants': ['OwnerStatusIndex', 'AgentDirectoryIndex', 'AgentReportsIndex'] } },
    );
    expect(code).toBe(1);
    expect(output).toContain('rag-assistants');
    expect(output).toContain('AgentDirectoryIndex');
    expect(output).toContain('AgentReportsIndex');
  });

  it('exempts a brand-new table with many indexes (CreateTable has no limit)', () => {
    const { code } = run(
      { tables: { existing: ['A'] } },
      { tables: { existing: ['A'], 'audit-log': ['X', 'Y', 'Z'] } },
    );
    expect(code).toBe(0);
  });

  it('fails when an existing table loses two indexes', () => {
    // The API limit counts deletions too, not just creations.
    const { code } = run({ tables: { t: ['A', 'B', 'C'] } }, { tables: { t: ['C'] } });
    expect(code).toBe(1);
  });

  it('fails on one addition combined with one removal in the same update', () => {
    // "more than one GSI creation OR DELETION in a single update" — a swap is
    // two operations and is rejected exactly like two creations.
    const { code } = run({ tables: { t: ['A'] } }, { tables: { t: ['B'] } });
    expect(code).toBe(1);
  });

  it('ignores a table dropped entirely (DeleteTable, not UpdateTable)', () => {
    const { code } = run({ tables: { gone: ['A', 'B', 'C'], kept: [] } }, { tables: { kept: [] } });
    expect(code).toBe(0);
  });

  it('passes on an unchanged inventory', () => {
    const { code } = run({ tables: { t: ['A', 'B'] } }, { tables: { t: ['A', 'B'] } });
    expect(code).toBe(0);
  });

  it('passes the committed inventory against itself', () => {
    const committed = JSON.parse(readFileSync(INVENTORY_PATH, 'utf8')) as Inventory;
    const { code } = run(committed, committed);
    expect(code).toBe(0);
  });
});
