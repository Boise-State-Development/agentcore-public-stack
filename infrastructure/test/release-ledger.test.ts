/**
 * Release ledger writes (docs/specs/release-ledger.md §5.2, D8). Pinned:
 *   - the Runtime gets RELEASE_LEDGER_ENABLED only while on, so an environment
 *     that leaves it off spends none of the V2 environment budget;
 *   - app-api always carries it ('false' by default), since it writes the
 *     api-converse cost rows;
 *   - turning it on changes no IAM and adds no resource: the `LEDGER#` rows
 *     live in the existing system-cost-rollup table, and both roles already
 *     hold UpdateItem on it.
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

  it('changes no IAM when turned on', () => {
    const policies = (t: Template) =>
      ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].map((type) => JSON.stringify(t.findResources(type)));
    expect(policies(on)).toEqual(policies(off));
  });

  it('adds no resource', () => {
    const count = (t: Template) => Object.keys(t.toJSON().Resources).length;
    expect(count(on)).toBe(count(off));
  });
});
