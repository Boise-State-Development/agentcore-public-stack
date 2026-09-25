/**
 * AgentCore Memory: the strategy set the backend depends on, and the Runtime
 * role's memory grants (Shared Projects Phase 0.2, docs/specs/memory-baseline-decision.md).
 *
 * The backend discovers strategy ids by TYPE at process start and builds its
 * retrieval namespaces from AWS's default templates
 * (`/strategies/{memoryStrategyId}/actors/{actorId}/…`, session_factory.py).
 * Renaming a strategy replaces it (and orphans its records); setting explicit
 * `namespaces` changes where records land. Either needs a backend change in
 * the same PR, so both are pinned here.
 *
 * The Runtime role carries two memory statements: account-wide
 * `AgentCoreMemoryAccess` and deployment-scoped `MemoryAccess`. The scoped one
 * once lacked GetMemory, which strategy discovery needs; retrieval only worked
 * because the wildcard statement had it. They now share one action list.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface PolicyStatement {
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Sid?: string;
}

function asArray(v: string | string[] | undefined): string[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

describe('AgentCore Memory', () => {
  let template: Template;
  let statementsBySid: Map<string, PolicyStatement[]>;

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

    // CDK may split an oversized inline policy into managed overflow policies,
    // so scan both resource types.
    statementsBySid = new Map();
    for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
      for (const [, r] of Object.entries(template.findResources(type))) {
        const stmts =
          (r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })
            ?.PolicyDocument?.Statement ?? [];
        for (const s of stmts) {
          if (!s.Sid) continue;
          statementsBySid.set(s.Sid, [...(statementsBySid.get(s.Sid) ?? []), s]);
        }
      }
    }
  });

  function memoryProps(): Record<string, unknown> {
    const memories = template.findResources('AWS::BedrockAgentCore::Memory');
    const entries = Object.values(memories);
    expect(entries).toHaveLength(1);
    return entries[0].Properties as Record<string, unknown>;
  }

  it('declares exactly the three strategies the backend discovers, by name', () => {
    const strategies = memoryProps().MemoryStrategies as Record<string, { Name: string }>[];
    const byType = Object.fromEntries(
      strategies.map((s) => {
        const [type, body] = Object.entries(s)[0];
        return [type, body.Name];
      }),
    );
    expect(byType).toEqual({
      SemanticMemoryStrategy: 'SemanticFactExtraction',
      SummaryMemoryStrategy: 'ConversationSummary',
      UserPreferenceMemoryStrategy: 'UserPreferenceExtraction',
    });
  });

  it('sets no namespace templates, so AWS defaults (which session_factory.py queries) apply', () => {
    const strategies = memoryProps().MemoryStrategies as Record<string, Record<string, unknown>>[];
    for (const s of strategies) {
      const body = Object.values(s)[0];
      expect(body.Namespaces).toBeUndefined();
    }
  });

  it('keeps events for 90 days', () => {
    expect(memoryProps().EventExpiryDuration).toBe(90);
  });

  it('grants the Runtime role the same memory actions in both statements, including GetMemory', () => {
    const actionsOf = (sid: string): string[] => {
      const stmts = statementsBySid.get(sid) ?? [];
      expect(stmts.length).toBeGreaterThan(0);
      return [...new Set(stmts.flatMap((s) => asArray(s.Action)))].sort();
    };
    const scoped = actionsOf('MemoryAccess');
    const wide = actionsOf('AgentCoreMemoryAccess');
    expect(scoped).toEqual(wide);
    expect(scoped).toContain('bedrock-agentcore:GetMemory');
    expect(scoped).toContain('bedrock-agentcore:RetrieveMemoryRecords');
  });
});
