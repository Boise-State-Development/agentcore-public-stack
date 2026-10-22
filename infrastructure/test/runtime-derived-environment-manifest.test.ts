// The derived-environment manifest must agree with CDK (docs/specs/agentcore-runtime-v2.md §7.3).
//
// backend/src/apis/shared/config/derived_environment.json lists the Runtime
// variables the inference-api rebuilds as `{prefix}-{suffix}[-{account}]` at
// startup. The Runtime is no longer sent them, so the manifest is now the ONLY
// thing that tells the Runtime where its tables and buckets are. This test is
// what keeps that honest: for every entry, the synthesized template must
// contain a resource CDK names exactly that, and the Runtime must not also be
// sent the variable (a value sent alongside would win at runtime and hide a
// drifted manifest). Renaming a resource in CDK without the manifest fails
// here, as does a manifest entry for a resource CDK names some other way.
import * as fs from 'fs';
import * as path from 'path';
import { MOCK_ACCOUNT, MOCK_PREFIX } from './helpers/mock-config';
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

interface ManifestEntry { name: string; suffix: string; accountScoped?: boolean }

const MANIFEST_PATH = path.resolve(__dirname, '../../backend/src/apis/shared/config/derived_environment.json');

/** Properties that carry a resource's physical name, across the types the manifest covers. */
const NAME_PROPERTIES = ['TableName', 'BucketName', 'VectorBucketName', 'IndexName', 'Name', 'SecretName'];

function loadManifest(): ManifestEntry[] {
  return (JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8')) as { variables: ManifestEntry[] }).variables;
}

/** A template value as a literal, substituting the mock account; undefined when it is not a plain name. */
function literal(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    if (record.Ref === 'AWS::AccountId') return MOCK_ACCOUNT;
    const join = record['Fn::Join'] as [string, unknown[]] | undefined;
    if (join && Array.isArray(join[1])) {
      const parts = join[1].map(literal);
      return parts.every((p) => p !== undefined) ? parts.join(join[0]) : undefined;
    }
  }
  return undefined;
}

describe('derived_environment.json agrees with the synthesized stack', () => {
  const manifest = loadManifest();
  const { template } = synthWorstCasePlatformStack();
  const env = runtimeEnvironment(template);

  const physicalNames = new Set<string>();
  for (const resource of Object.values(template.toJSON().Resources ?? {}) as Array<{ Properties?: Record<string, unknown> }>) {
    for (const key of NAME_PROPERTIES) {
      const name = literal(resource.Properties?.[key]);
      if (name) physicalNames.add(name);
    }
  }

  it('lists unique entries', () => {
    expect(manifest.length).toBeGreaterThanOrEqual(20);
    expect(new Set(manifest.map((m) => m.name)).size).toBe(manifest.length);
  });

  it.each(manifest.map((m) => [m.name, m] as const))('%s: CDK names a resource {prefix}-{suffix}[-{account}]', (_name, entry) => {
    const expected = entry.accountScoped
      ? `${MOCK_PREFIX}-${entry.suffix}-${MOCK_ACCOUNT}`
      : `${MOCK_PREFIX}-${entry.suffix}`;
    expect(physicalNames).toContain(expected);
  });

  it.each(manifest.map((m) => [m.name] as const))('%s is derived, not sent to the Runtime', (name) => {
    expect(env).not.toHaveProperty(name);
  });

  it('sends the two inputs the derivation needs', () => {
    expect(env.PROJECT_PREFIX).toBe(MOCK_PREFIX);
    expect(env.AWS_ACCOUNT_ID).toBe(MOCK_ACCOUNT);
  });
});
