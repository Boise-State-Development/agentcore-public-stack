/**
 * Which DynamoDB tables the app-api task role and the AgentCore Runtime role
 * can reach, evaluated the way IAM would against the synthesized tables.
 *
 * app-api's grant is one `table/{prefix}-*` statement and the Runtime's entries
 * are `<tableArn>*` patterns (deployment-table-grants.ts, inference-api-iam-
 * roles.ts), so asserting on per-table statement Sids no longer says anything.
 * This file asserts the outcome instead: for every table, which item actions
 * each role is allowed once its Allows and Denies are combined. It pins both
 * directions — every table a role used before the prefix grants is still
 * reachable, and the Runtime reaches nothing it could not reach before.
 */
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig, getProjectTag } from '../lib/config';
import {
  otherDeploymentsTablesDeny,
  TABLE_READ_WRITE_ACTIONS,
} from '../lib/constructs/data/deployment-table-grants';
import { createMockConfig } from './helpers/mock-config';
import { synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

interface Statement {
  Sid?: string;
  Effect: 'Allow' | 'Deny';
  Action: string | string[];
  Resource: unknown;
  Condition?: Record<string, Record<string, string | string[]>>;
}

interface Table {
  name: string;
  arn: string;
  indexes: string[];
  projectTag: string | undefined;
}

const asArray = <T>(v: T | T[]): T[] => (Array.isArray(v) ? v : [v]);

/** IAM's wildcard match: `*` is any run of characters (including `/`), `?` one. */
function globMatches(pattern: string, value: string): boolean {
  const regex = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp(`^${regex}$`).test(value);
}

/**
 * Evaluate a statement's condition against a table's tags. Only the operators
 * these roles' table statements use are understood; anything else throws, so a
 * new condition has to be modelled here rather than silently read as true.
 */
function conditionHolds(condition: Statement['Condition'], tag: string | undefined): boolean {
  if (!condition) return true;
  return Object.entries(condition).every(([operator, keys]) =>
    Object.entries(keys).every(([key, expected]) => {
      if (key !== 'aws:ResourceTag/Project') {
        throw new Error(`unmodelled condition key ${key}`);
      }
      if (operator === 'Null') return (tag === undefined) === (expected === 'true');
      if (operator === 'StringNotEquals') return tag !== undefined && !asArray(expected).includes(tag);
      throw new Error(`unmodelled condition operator ${operator}`);
    }));
}

describe('DynamoDB table grants', () => {
  let config: AppConfig;
  let template: Template;
  let tables: Table[];
  let resolve: (node: unknown) => string;
  let statementsFor: (roleIdFragment: string) => Statement[];

  beforeAll(() => {
    ({ config, template } = synthWorstCasePlatformStack());
    const resources = template.toJSON().Resources as Record<string, any>;
    const arnOf = (name: string): string =>
      `arn:aws:dynamodb:${config.awsRegion}:${config.awsAccount}:table/${name}`;

    tables = Object.values(resources)
      .filter((r) => r.Type === 'AWS::DynamoDB::Table')
      .map((r) => ({
        name: r.Properties.TableName,
        arn: arnOf(r.Properties.TableName),
        indexes: (r.Properties.GlobalSecondaryIndexes ?? []).map((g: any) => g.IndexName),
        projectTag: (r.Properties.Tags ?? []).find((t: any) => t.Key === 'Project')?.Value,
      }));

    resolve = (node: unknown): string => {
      if (typeof node === 'string') return node;
      const n = node as Record<string, any>;
      if (n['Fn::GetAtt']) {
        const [logicalId, attribute] = n['Fn::GetAtt'];
        const target = resources[logicalId];
        if (target?.Type !== 'AWS::DynamoDB::Table' || attribute !== 'Arn') {
          return `unresolved:${logicalId}.${attribute}`;
        }
        return arnOf(target.Properties.TableName);
      }
      if (n['Fn::Join']) {
        const [separator, parts] = n['Fn::Join'];
        return (parts as unknown[]).map(resolve).join(separator);
      }
      if (n.Ref === 'AWS::Region') return config.awsRegion;
      if (n.Ref === 'AWS::AccountId') return config.awsAccount;
      if (n.Ref === 'AWS::Partition') return 'aws';
      return `unresolved:${JSON.stringify(node)}`;
    };

    statementsFor = (roleIdFragment: string): Statement[] => {
      const roleIds = Object.keys(resources)
        .filter((id) => resources[id].Type === 'AWS::IAM::Role' && id.includes(roleIdFragment));
      expect(roleIds).toHaveLength(1);
      return Object.values(resources)
        .filter((r) => r.Type === 'AWS::IAM::Policy' || r.Type === 'AWS::IAM::ManagedPolicy')
        .filter((r) => JSON.stringify(r.Properties.Roles ?? []).includes(`"${roleIds[0]}"`))
        .flatMap((r) => r.Properties.PolicyDocument.Statement as Statement[]);
    };
  });

  /** What a role may do to one ARN: some unconditional Allow matches, and no Deny does. */
  function allows(statements: Statement[], action: string, arn: string, tag: string | undefined): boolean {
    const matches = (s: Statement): boolean =>
      asArray(s.Action).some((a) => globMatches(a, action))
      && asArray(s.Resource).map(resolve).some((r) => globMatches(r, arn));
    const allowed = statements.some((s) => s.Effect === 'Allow' && !s.Condition && matches(s));
    const denied = statements.some((s) => s.Effect === 'Deny' && matches(s) && conditionHolds(s.Condition, tag));
    return allowed && !denied;
  }

  function allowedActions(statements: Statement[], table: Table, arn = table.arn): string[] {
    return TABLE_READ_WRITE_ACTIONS.filter((a) => allows(statements, a, arn, table.projectTag));
  }

  it('measures a whole stack: every table is named from the prefix and tagged for this deployment', () => {
    expect(tables.length).toBeGreaterThanOrEqual(29);
    for (const table of tables) {
      expect(table.name.startsWith(`${config.projectPrefix}-`)).toBe(true);
      // The Deny that closes the prefix collision refuses a table whose
      // Project tag differs, so a table missing this tag, or carrying another
      // value, is the one case where it would refuse this deployment's own.
      expect(table.projectTag).toBe(getProjectTag(config));
    }
  });

  it('app-api role reaches every table, and every index on it, with read and write', () => {
    const statements = statementsFor('AppApiAppApiTaskDefinitionTaskRole');
    for (const table of tables) {
      expect({ table: table.name, actions: allowedActions(statements, table) })
        .toEqual({ table: table.name, actions: [...TABLE_READ_WRITE_ACTIONS] });
      for (const index of table.indexes) {
        expect({ index: `${table.name}/${index}`, query: allows(statements, 'dynamodb:Query', `${table.arn}/index/${index}`, table.projectTag) })
          .toEqual({ index: `${table.name}/${index}`, query: true });
      }
    }
  });

  /**
   * Exactly what the Runtime held before its grants were compacted, by table
   * suffix. A table missing here gets no access at all; `indexes` means a
   * Query on each of its GSIs.
   */
  const RUNTIME_ACCESS: Record<string, { actions: string[]; indexes: boolean }> = {
    ...Object.fromEntries([
      'users', 'app-roles', 'oauth-providers', 'oauth-user-tokens', 'api-keys', 'rag-assistants',
      'user-quotas', 'quota-events', 'sessions-metadata', 'user-cost-summary', 'system-cost-rollup',
      'managed-models', 'auth-providers', 'user-file-uploads',
    ].map((suffix) => [suffix, { actions: [...TABLE_READ_WRITE_ACTIONS], indexes: true }])),
    'user-settings': { actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'], indexes: false },
    'system-prompts': { actions: ['dynamodb:GetItem'], indexes: false },
    'user-artifacts': {
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
      indexes: true,
    },
    'memory-spaces': {
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem', 'dynamodb:Query',
        'dynamodb:BatchWriteItem'],
      indexes: true,
    },
    projects: {
      actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'],
      indexes: true,
    },
    'shared-conversations': { actions: ['dynamodb:GetItem'], indexes: false },
  };

  it('Runtime role reaches the tables it uses, with exactly the actions it held, and no others', () => {
    // Conditional Allows (ProjectsInboxWrite's INBOX# keys, MemorySpacesStatsUpdate's
    // attributes) are left out: they are pinned by projects-table.test.ts and
    // runtime-memory-stats-grant.test.ts. What is checked here is unconditional access.
    const statements = statementsFor('AgentCoreRuntimeExecutionRole');
    for (const table of tables) {
      const suffix = table.name.slice(config.projectPrefix.length + 1);
      const expected = RUNTIME_ACCESS[suffix] ?? { actions: [], indexes: false };
      expect({ table: table.name, actions: allowedActions(statements, table).sort() })
        .toEqual({ table: table.name, actions: [...expected.actions].sort() });
      for (const index of table.indexes) {
        const query = allows(statements, 'dynamodb:Query', `${table.arn}/index/${index}`, table.projectTag);
        expect({ index: `${table.name}/${index}`, query })
          .toEqual({ index: `${table.name}/${index}`, query: expected.indexes });
      }
    }
    // Non-vacuous: the sensitive tables the Runtime must never reach exist here.
    for (const suffix of ['bff-sessions', 'oidc-state', 'audit-log']) {
      expect(tables.map((t) => t.name)).toContain(`${config.projectPrefix}-${suffix}`);
    }
  });

  it('every `<tableArn>*` entry on the Runtime role names exactly one table', () => {
    // The trailing `*` folds a table's `/index/*` into its own entry, and would
    // also match a later table whose name extends this one's. Such a table
    // fails here rather than reaching the Runtime unreviewed.
    const statements = statementsFor('AgentCoreRuntimeExecutionRole');
    const patterns = statements
      .filter((s) => s.Effect === 'Allow')
      .flatMap((s) => asArray(s.Resource).map(resolve))
      .filter((r) => r.includes(':dynamodb:') && r.endsWith('*') && !r.endsWith('/index/*'));
    expect(patterns.length).toBeGreaterThan(0);
    for (const pattern of patterns) {
      const named = tables.filter((t) => globMatches(pattern, t.arn)).map((t) => t.name);
      expect({ pattern, named: named.length }).toEqual({ pattern, named: 1 });
    }
  });

  describe('the prefix collision', () => {
    // `projectPrefix` allows hyphens, so a second deployment prefixed
    // `{prefix}-other` in the same account names tables the prefix patterns match.
    const foreign = (config: AppConfig, suffix: string): string =>
      `arn:aws:dynamodb:${config.awsRegion}:${config.awsAccount}:table/${config.projectPrefix}-other-${suffix}`;

    for (const [label, roleIdFragment] of [
      ['app-api', 'AppApiAppApiTaskDefinitionTaskRole'],
      ['Runtime', 'AgentCoreRuntimeExecutionRole'],
    ]) {
      it(`${label} role is refused another deployment's tables`, () => {
        const statements = statementsFor(roleIdFragment);
        // `-other-users` for app-api's `{prefix}-*`; `users-other` for the
        // Runtime's `{prefix}-users*`, which only a prefix extending the table
        // name could produce.
        const arns = [
          foreign(config, 'users'),
          `arn:aws:dynamodb:${config.awsRegion}:${config.awsAccount}:table/${config.projectPrefix}-users-other-users`,
        ];
        for (const arn of arns) {
          for (const action of TABLE_READ_WRITE_ACTIONS) {
            expect({ arn, action, allowed: allows(statements, action, arn, `${config.projectPrefix}-other`) })
              .toEqual({ arn, action, allowed: false });
          }
        }
      });
    }

    it('fails open, not closed, where DynamoDB ABAC is off', () => {
      // An account with ABAC off evaluates tag conditions as if no tags exist.
      // The Deny then never fires and the grant is the prefix pattern alone,
      // the documented residual exposure, rather than refusing every table,
      // which an Allow conditioned on the tag would do.
      const statements = statementsFor('AppApiAppApiTaskDefinitionTaskRole');
      expect(allows(statements, 'dynamodb:GetItem', foreign(config, 'users'), undefined)).toBe(true);
      const own = tables[0];
      expect(allows(statements, 'dynamodb:GetItem', own.arn, undefined)).toBe(true);
    });

    it("compares against the Project tag actually applied, including a deployment's own override", () => {
      const overridden = createMockConfig({ tags: { Project: 'shared-tag' } });
      expect(getProjectTag(overridden)).toBe('shared-tag');
      expect(getProjectTag(createMockConfig({ tags: {} }))).toBe(createMockConfig().projectPrefix);
      const deny = otherDeploymentsTablesDeny(overridden, 'Probe').toStatementJson();
      expect(deny.Condition).toEqual({
        Null: { 'aws:ResourceTag/Project': 'false' },
        StringNotEquals: { 'aws:ResourceTag/Project': 'shared-tag' },
      });
    });
  });
});
