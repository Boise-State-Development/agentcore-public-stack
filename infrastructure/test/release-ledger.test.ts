/**
 * Release ledger writes (docs/specs/release-ledger.md §5.2, D8). Pinned:
 *   - the Runtime gets RELEASE_LEDGER_ENABLED only while on, so an environment
 *     that leaves it off spends none of the V2 environment budget;
 *   - app-api always carries it ('false' by default), since it writes the
 *     api-converse cost rows;
 *   - turning it on adds no resource, and one IAM statement: the `LEDGER#`
 *     rows live in the existing system-cost-rollup table, which both roles
 *     already read and write, and app-api gains `cloudwatch:GetMetricData`
 *     for the readout's TTFT percentiles (§6.1), on a policy that exists.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { AppConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function synth(overrides: Partial<AppConfig> = {}): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function runtimeEnv(template: Template): Record<string, unknown> {
  const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
  expect(runtimes).toHaveLength(1);
  return (runtimes[0] as any).Properties.EnvironmentVariables;
}

function appApiEnv(template: Template): Record<string, unknown> {
  const taskDefs = Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[];
  const env: Record<string, unknown> = {};
  for (const td of taskDefs) {
    for (const container of td.Properties.ContainerDefinitions ?? []) {
      for (const { Name, Value } of container.Environment ?? []) env[Name] = Value;
    }
  }
  return env;
}

describe('release ledger wiring', () => {
  const off = synth();
  const on = synth({ releaseLedger: { enabled: true } });

  it('spends no Runtime env bytes while off, and sets the flag only while on', () => {
    expect(runtimeEnv(off)).not.toHaveProperty('RELEASE_LEDGER_ENABLED');
    expect(runtimeEnv(on).RELEASE_LEDGER_ENABLED).toBe('true');
  });

  it('sets the flag on app-api either way', () => {
    expect(appApiEnv(off).RELEASE_LEDGER_ENABLED).toBe('false');
    expect(appApiEnv(on).RELEASE_LEDGER_ENABLED).toBe('true');
  });

  it('adds exactly one IAM statement when turned on: GetMetricData for the readout', () => {
    const statements = (t: Template) => {
      const out: any[] = [];
      for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
        for (const resource of Object.values(t.findResources(type)) as any[]) {
          out.push(...resource.Properties.PolicyDocument.Statement);
        }
      }
      return out;
    };
    const offSids = new Set(statements(off).map((s) => JSON.stringify(s)));
    const added = statements(on).filter((s) => !offSids.has(JSON.stringify(s)));
    expect(added).toEqual([
      { Sid: 'ReleaseLedgerMetricsRead', Effect: 'Allow', Action: 'cloudwatch:GetMetricData', Resource: '*' },
    ]);
    expect(statements(on)).toHaveLength(statements(off).length + 1);
  });

  it('adds no resource', () => {
    const count = (t: Template) => Object.keys(t.toJSON().Resources).length;
    expect(count(on)).toBe(count(off));
  });
});
