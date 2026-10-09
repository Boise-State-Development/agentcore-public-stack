/**
 * The Runtime's grant to count memory file reads (Shared Projects 2.6b, `STATS#{slug}`).
 *
 * A project harness's `memory_read` adds to a counter on the file's stats row:
 * one UpdateItem. The Runtime role had no UpdateItem on the memory-spaces table,
 * and must not gain an unconditioned one, which could rewrite a manifest or a
 * file version. So the grant is its own managed policy, pinned by
 * `dynamodb:Attributes` to the stats row's attributes, and the role's default
 * and CDK overflow policies still have no UpdateItem on that table (an
 * overflow policy was 5,473 of IAM's 6,144 characters on dev).
 *
 * Compute roles exist only after `wireCompute()`, which `platform-stack.test.ts`
 * never calls, so this test builds its own wired stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { MEMORY_STATS_ATTRIBUTES } from '../lib/constructs/inference-api/inference-api-iam-roles';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface Statement {
  Sid?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
}

function actions(st: Statement): string[] {
  return Array.isArray(st.Action) ? st.Action : [st.Action ?? ''];
}

function onMemoryTable(st: Statement): boolean {
  return JSON.stringify(st.Resource ?? '').includes('MemorySpacesTable');
}

describe('Runtime memory stats grant', () => {
  let template: Template;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  it('is a one-statement managed policy on the Runtime role, pinned to the stats attributes', () => {
    const policies = Object.entries(template.findResources('AWS::IAM::ManagedPolicy')).filter(([id]) =>
      id.includes('RuntimeMemoryStatsPolicy'),
    );
    expect(policies).toHaveLength(1);
    const [, policy] = policies[0];
    const statements: Statement[] = policy.Properties.PolicyDocument.Statement;
    expect(statements).toHaveLength(1);
    const [st] = statements;
    expect(actions(st)).toEqual(['dynamodb:UpdateItem']);
    expect(onMemoryTable(st)).toBe(true);
    // The table itself, not its indexes: UpdateItem never targets an index.
    expect(JSON.stringify(st.Resource)).not.toContain('/index/');
    expect(st.Condition?.['ForAllValues:StringEquals']?.['dynamodb:Attributes']).toEqual([
      'PK',
      'SK',
      'slug',
      'retrievalCount',
      'lastRetrievedAt',
    ]);
    expect(MEMORY_STATS_ATTRIBUTES).toHaveLength(5);
    expect(st.Condition?.StringEqualsIfExists?.['dynamodb:ReturnValues']).toBe('NONE');
    expect(JSON.stringify(policy.Properties.Roles)).toContain('AgentCoreRuntimeExecutionRole');
  });

  it('adds no UpdateItem on the memory table to the Runtime role default or overflow policies', () => {
    const roleOwn = [
      ...Object.entries(template.findResources('AWS::IAM::Policy')),
      ...Object.entries(template.findResources('AWS::IAM::ManagedPolicy')),
    ].filter(([id]) => id.includes('AgentCoreRuntimeExecutionRole'));
    const statements: Statement[] = roleOwn.flatMap(([, r]) => r.Properties.PolicyDocument?.Statement ?? []);
    // Non-vacuous: the role's own policies, and its memory-table statement, are in the template.
    expect(statements.some((st) => st.Sid === 'MemorySpacesTableReadWrite')).toBe(true);
    const updates = statements.filter((st) => onMemoryTable(st) && actions(st).includes('dynamodb:UpdateItem'));
    expect(updates).toEqual([]);
  });
});
