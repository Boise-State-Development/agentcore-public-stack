// The derived-environment manifest must agree with CDK (docs/specs/agentcore-runtime-v2.md §7.3).
//
// backend/src/apis/shared/config/derived_environment.json says which Runtime
// variables the code can rebuild as `{prefix}-{suffix}[-{account}]`. The
// resolver trusts it; this test is what keeps that trust honest. For every
// entry it resolves the Runtime's variable in the synthesized template to a
// literal and asserts it equals the derivation. Renaming a resource in CDK
// without updating the manifest fails here, as does a manifest entry for a
// resource CDK names some other way (getTruncatedResourceName, a physical id).
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { estimateRuntimeEnvironmentPayload } from '../lib/constructs/inference-api/runtime-environment-payload-guard';
import { MOCK_ACCOUNT, MOCK_PREFIX } from './helpers/mock-config';
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

interface ManifestEntry { name: string; suffix: string; accountScoped?: boolean }

const MANIFEST_PATH = path.resolve(__dirname, '../../backend/src/apis/shared/config/derived_environment.json');

function loadManifest(): ManifestEntry[] {
  const raw = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8')) as { variables: ManifestEntry[] };
  return raw.variables;
}

/** Resolve one template value to a literal string the way the Python side would see it at runtime. */
function resolveLiteral(stack: cdk.Stack, value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    if (typeof record.Ref === 'string') {
      if (record.Ref === 'AWS::AccountId') return MOCK_ACCOUNT;
      for (const child of stack.node.findAll()) {
        if (!cdk.CfnResource.isCfnResource(child)) continue;
        if (stack.resolve(child.logicalId) !== record.Ref) continue;
        const rendered = stack.resolve(
          (child as unknown as { _toCloudFormation(): { Resources?: Record<string, { Properties?: Record<string, unknown> }> } })._toCloudFormation(),
        );
        const props = rendered?.Resources?.[record.Ref]?.Properties ?? {};
        for (const key of ['TableName', 'BucketName', 'SecretName', 'Name']) {
          if (props[key] !== undefined) return resolveLiteral(stack, props[key]);
        }
      }
      throw new Error(`Ref ${record.Ref} is not a resource with a literal name`);
    }
    const join = record['Fn::Join'] as [string, unknown[]] | undefined;
    if (join) return join[1].map((part) => resolveLiteral(stack, part)).join(join[0]);
  }
  throw new Error(`cannot resolve ${JSON.stringify(value)} to a literal`);
}

describe('derived_environment.json agrees with the synthesized Runtime', () => {
  const manifest = loadManifest();
  const { stack, template } = synthWorstCasePlatformStack();
  const env = runtimeEnvironment(template);

  it('lists at least the variables the plan expects to derive', () => {
    expect(manifest.length).toBeGreaterThanOrEqual(20);
    expect(new Set(manifest.map((m) => m.name)).size).toBe(manifest.length);
  });

  it.each(manifest.map((m) => [m.name, m] as const))('%s is {prefix}-{suffix}[-{account}] in the template', (_name, entry) => {
    expect(env).toHaveProperty(entry.name);
    const literal = resolveLiteral(stack, env[entry.name]);
    const expected = entry.accountScoped
      ? `${MOCK_PREFIX}-${entry.suffix}-${MOCK_ACCOUNT}`
      : `${MOCK_PREFIX}-${entry.suffix}`;
    expect(literal).toBe(expected);
  });

  it('reports how much of the payload the manifest would remove', () => {
    const all = estimateRuntimeEnvironmentPayload(stack, env as Record<string, string>);
    const remaining = Object.fromEntries(Object.entries(env).filter(([k]) => !manifest.some((m) => m.name === k)));
    const after = estimateRuntimeEnvironmentPayload(stack, remaining as Record<string, string>);
    // Not an assertion: the number the next PR (spec §7.6 PR B) is sized by.
    console.log(
      `[derived-env] manifest covers ${manifest.length} of ${all.variables} Runtime variables; ` +
      `~${all.bytes} → ~${after.bytes} estimated bytes once they are dropped (plus ~${'AWS_ACCOUNT_ID'.length + 12 + 2} for AWS_ACCOUNT_ID)`,
    );
    expect(after.bytes).toBeLessThan(all.bytes);
  });
});
